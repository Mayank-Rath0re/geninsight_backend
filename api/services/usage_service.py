# services/usage_service.py
"""
Usage metering and limit enforcement.

  check_quota(user_id)         -> call BEFORE any LLM work; raises 429 when blocked
  metered_call(user_id, kind)  -> wrap the LLM work; persists tokens even if it fails
  assert_can_upload(user_id)   -> file-cap check for uploads
  snapshot(user_id)            -> read-only view used by GET /billing/me

Windows: session = 5h and day = 24h, each opened by the first metered call after the
previous window expired. Month = calendar month (UTC). Limits are hard-blocked before a
call; an in-flight call may overshoot the cap slightly and the next call is blocked.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from core import db
from core.usage_context import UsageCollector, set_collector, reset_collector
from services import billing_service
from services.billing_service import Entitlement, Plan, utcnow

logger = logging.getLogger("my_global_app_logger")

SESSION_WINDOW = timedelta(hours=5)
DAY_WINDOW = timedelta(hours=24)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def _next_month(d: date) -> datetime:
    return datetime(d.year + (d.month == 12), (d.month % 12) + 1, 1)


@dataclass
class Snapshot:
    session_tokens: int
    session_resets_at: Optional[datetime]
    day_tokens: int
    day_resets_at: Optional[datetime]
    month_queries: int
    month_resets_at: datetime


def _rolled(row, now: datetime) -> tuple:
    """Apply window expiry to a raw counters row. Returns normalised values."""
    s_start, s_tok, d_start, d_tok, m_start, m_q = row
    if s_start is None or now >= s_start + SESSION_WINDOW:
        s_start, s_tok = None, 0
    if d_start is None or now >= d_start + DAY_WINDOW:
        d_start, d_tok = None, 0
    this_month = date(now.year, now.month, 1)
    if m_start is None or m_start != this_month:
        m_start, m_q = this_month, 0
    return s_start, int(s_tok), d_start, int(d_tok), m_start, int(m_q)


_COUNTER_COLS = ("session_started_at, session_tokens, day_started_at, day_tokens, "
                 "month_start, month_queries")


def snapshot(user_id: int, now: Optional[datetime] = None) -> Snapshot:
    now = now or utcnow()
    rows = db.fetch(f"SELECT {_COUNTER_COLS} FROM usage_counters WHERE user_id = %s", (user_id,))
    raw = tuple(rows[0]) if rows else (None, 0, None, 0, None, 0)
    s_start, s_tok, d_start, d_tok, m_start, m_q = _rolled(raw, now)
    return Snapshot(
        session_tokens=s_tok,
        session_resets_at=s_start + SESSION_WINDOW if s_start else None,
        day_tokens=d_tok,
        day_resets_at=d_start + DAY_WINDOW if d_start else None,
        month_queries=m_q,
        month_resets_at=_next_month(m_start),
    )


def compute_halt(plan: Plan, snap: Snapshot, include_queries: bool = True) -> Optional[dict]:
    """Return the blocking limit that lifts last, or None."""
    hits = []
    if (include_queries and plan.monthly_query_limit is not None
            and snap.month_queries >= plan.monthly_query_limit):
        hits.append(("monthly_queries", snap.month_resets_at))
    if (plan.daily_token_limit is not None and snap.day_tokens >= plan.daily_token_limit
            and snap.day_resets_at):
        hits.append(("daily", snap.day_resets_at))
    if (plan.session_token_limit is not None and snap.session_tokens >= plan.session_token_limit
            and snap.session_resets_at):
        hits.append(("session", snap.session_resets_at))
    if not hits:
        return None
    reason, resumes = max(hits, key=lambda h: h[1])
    return {"reason": reason, "resumes_at": iso(resumes)}


def check_quota(user_id: int, include_queries: bool = True) -> tuple[Entitlement, Snapshot]:
    """
    Raise 429 if the user is blocked. include_queries=False ignores the Free monthly query cap
    (used for uploads, which spend tokens but are not "queries").
    """
    ent = billing_service.get_entitlement(user_id)
    snap = snapshot(user_id)
    halt = compute_halt(ent.plan, snap, include_queries=include_queries)
    if halt:
        raise HTTPException(
            status_code=429,
            detail={"code": "USAGE_LIMIT", "message": "Usage limit reached.",
                    "plan": ent.plan.code, **halt},
        )
    return ent, snap


def files_used(user_id: int) -> int:
    n = db.get_single_value(
        "SELECT COUNT(*) FROM table_info "
        "WHERE user_id = %s AND is_deleted = 0 AND data_source_id IS NULL",
        (user_id,),
    )
    return int(n or 0)


def assert_can_upload(user_id: int) -> Entitlement:
    ent = billing_service.get_entitlement(user_id)
    used = files_used(user_id)
    if used >= ent.plan.max_files:
        raise HTTPException(
            status_code=403,
            detail={"code": "FILE_LIMIT", "plan": ent.plan.code,
                    "limit": ent.plan.max_files, "used": used,
                    "message": f"Your {ent.plan.name} plan allows {ent.plan.max_files} files."},
        )
    return ent


def require_feature(user_id: int, feature: str) -> Entitlement:
    """Gate plan-specific features (e.g. 'analysis', 'storytelling', 'self_db')."""
    ent = billing_service.get_entitlement(user_id)
    if not ent.plan.features.get(feature):
        raise HTTPException(
            status_code=403,
            detail={"code": "FEATURE_LOCKED", "feature": feature, "plan": ent.plan.code,
                    "message": f"{feature.replace('_', ' ').title()} is not included in your {ent.plan.name} plan."},
        )
    return ent


def record_usage(user_id: int, kind: str, c: UsageCollector,
                 trace_id: Optional[str] = None) -> None:
    now = utcnow()
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_COUNTER_COLS} FROM usage_counters WITH (UPDLOCK, HOLDLOCK) "
            "WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO usage_counters (user_id) VALUES (%s)", (user_id,))
            row = (None, 0, None, 0, None, 0)

        s_start, s_tok, d_start, d_tok, m_start, m_q = _rolled(tuple(row), now)
        if c.total > 0:
            s_start = s_start or now
            d_start = d_start or now
            s_tok += c.total
            d_tok += c.total
        if c.count_query:
            m_q += 1

        cur.execute(
            "UPDATE usage_counters SET session_started_at=%s, session_tokens=%s, "
            "day_started_at=%s, day_tokens=%s, month_start=%s, month_queries=%s, "
            "updated_at=SYSUTCDATETIME() WHERE user_id=%s",
            (s_start, s_tok, d_start, d_tok, m_start, m_q, user_id))
        cur.execute(
            "INSERT INTO usage_events (user_id, kind, input_tokens, output_tokens, total_tokens, "
            "llm_calls, counted_query, trace_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, kind, c.input_tokens, c.output_tokens, c.total, c.llm_calls,
             1 if c.count_query else 0, trace_id))


@contextmanager
def metered_call(user_id: int, kind: str, trace_id: Optional[str] = None):
    """
    Collects LLM tokens for the enclosed work and persists them on exit, even when the
    work raises (the provider already charged for those tokens). Set
    `meter.count_query = True` on success to count toward the Free monthly query cap.
    Enter this INSIDE the endpoint function (see core/usage_context.py).
    """
    collector = UsageCollector()
    token = set_collector(collector)
    try:
        yield collector
    finally:
        reset_collector(token)
        if collector.total > 0 or collector.count_query:
            try:
                record_usage(user_id, kind, collector, trace_id)
            except Exception:
                logger.exception("Failed to record usage for user %s", user_id)
