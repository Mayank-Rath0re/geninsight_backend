# services/billing_service.py
"""Plan catalog + entitlement resolution (who is on which plan right now)."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import db

# Keep access this long after period_end if the renewal webhook is late.
RENEWAL_GRACE = timedelta(days=2)

PLAN_COLS = (
    "id, code, name, price, currency, per_seat, min_seats, max_files, "
    "daily_token_limit, session_token_limit, monthly_query_limit, "
    "retention_days, max_data_sources, features"
)


def utcnow() -> datetime:
    """Naive UTC, matching what pymssql returns for DATETIME2 columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Plan:
    id: int
    code: str
    name: str
    price: float
    currency: str
    per_seat: bool
    min_seats: int
    max_files: int
    daily_token_limit: Optional[int]
    session_token_limit: Optional[int]
    monthly_query_limit: Optional[int]
    retention_days: Optional[int]
    max_data_sources: Optional[int]
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "price": self.price,
            "currency": self.currency, "per_seat": self.per_seat,
            "min_seats": self.min_seats, "max_files": self.max_files,
            "daily_token_limit": self.daily_token_limit,
            "session_token_limit": self.session_token_limit,
            "monthly_query_limit": self.monthly_query_limit,
            "retention_days": self.retention_days,
            "max_data_sources": self.max_data_sources,
            "features": self.features,
        }


def _plan(row) -> Plan:
    return Plan(
        id=row[0], code=row[1], name=row[2], price=float(row[3]), currency=row[4],
        per_seat=bool(row[5]), min_seats=row[6], max_files=row[7],
        daily_token_limit=row[8], session_token_limit=row[9],
        monthly_query_limit=row[10], retention_days=row[11],
        max_data_sources=row[12], features=json.loads(row[13]) if row[13] else {},
    )


def list_plans() -> list[Plan]:
    rows = db.fetch(f"SELECT {PLAN_COLS} FROM plans WHERE is_active = 1 ORDER BY price")
    return [_plan(r) for r in rows]


def get_plan_by_code(code: str) -> Optional[Plan]:
    rows = db.fetch(f"SELECT {PLAN_COLS} FROM plans WHERE code = %s AND is_active = 1", (code,))
    return _plan(rows[0]) if rows else None


def get_plan_by_id(plan_id: int) -> Optional[Plan]:
    rows = db.fetch(f"SELECT {PLAN_COLS} FROM plans WHERE id = %s", (plan_id,))
    return _plan(rows[0]) if rows else None


@dataclass
class Entitlement:
    plan: Plan
    seats: int = 1
    subscription_id: Optional[int] = None
    status: str = "free"
    period_end: Optional[datetime] = None
    cancel_at_period_end: bool = False
    org_id: Optional[int] = None
    is_owner: bool = True


def _is_entitled(status: str, period_end: Optional[datetime],
                 cancel_at_period_end: bool, now: datetime) -> bool:
    if status == "active":
        return period_end is None or now < period_end + RENEWAL_GRACE
    if status == "cancelled":
        # Cancelled-at-cycle-end keeps access until the paid period runs out.
        return bool(cancel_at_period_end) and period_end is not None and now < period_end
    return False


def get_entitlement(user_id: int) -> Entitlement:
    """
    Effective plan for a user: the best entitled subscription they own, or that
    belongs to an org they are an active member of. Falls back to Free.
    """
    now = utcnow()
    rows = db.fetch(
        """
        SELECT s.id, s.plan_id, s.status, s.seats, s.current_period_end,
               s.cancel_at_period_end, s.org_id, s.owner_user_id
        FROM subscriptions s
        WHERE s.status IN ('active', 'cancelled')
          AND (s.owner_user_id = %s
               OR s.org_id IN (SELECT org_id FROM org_members
                               WHERE user_id = %s AND status = 'active'))
        """,
        (user_id, user_id),
    )
    best: Optional[Entitlement] = None
    for sid, plan_id, status, seats, period_end, cancel_flag, org_id, owner_id in rows:
        if not _is_entitled(status, period_end, bool(cancel_flag), now):
            continue
        plan = get_plan_by_id(plan_id)
        if plan is None:
            continue
        cand = Entitlement(plan=plan, seats=seats, subscription_id=sid, status=status,
                           period_end=period_end, cancel_at_period_end=bool(cancel_flag),
                           org_id=org_id, is_owner=(owner_id == user_id))
        if best is None or plan.price > best.plan.price:
            best = cand
    if best is not None:
        return best

    free = get_plan_by_code("free")
    if free is None:
        raise RuntimeError("Free plan is missing from the plans table (seed not applied).")
    return Entitlement(plan=free)
