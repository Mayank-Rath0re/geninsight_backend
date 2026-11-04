import hashlib
import hmac
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core import context_propagation, db
from core.usage_context import UsageCollector, record_llm_usage, set_collector, reset_collector
from services import billing_service, razorpay_service, usage_service, retention
from services.billing_service import Plan, Entitlement

NOW = datetime(2026, 11, 3, 12, 0, 0)


def plan(code="individual", **kw):
    base = dict(id=2, code=code, name=code.title(), price=25.0, currency="USD", per_seat=True,
                min_seats=1, max_files=30, daily_token_limit=1_000_000, session_token_limit=250_000,
                monthly_query_limit=None, retention_days=365, max_data_sources=0, features={})
    base.update(kw)
    return Plan(**base)


FREE = plan("free", id=1, price=0, per_seat=False, daily_token_limit=None, session_token_limit=None,
            monthly_query_limit=50, retention_days=90, max_files=5)


# ───────────────────────── window rollover / halting ─────────────────────────

def test_session_window_expires_after_5_hours():
    started = NOW - timedelta(hours=5, minutes=1)
    s, tok, d, dtok, m, q = usage_service._rolled((started, 200_000, NOW - timedelta(hours=6), 200_000,
                                                   date(2026, 11, 1), 3), NOW)
    assert (s, tok) == (None, 0)            # session reset
    assert dtok == 200_000                  # day window (24h) still open
    assert q == 3


def test_month_rollover_resets_queries():
    *_, m, q = usage_service._rolled((None, 0, None, 0, date(2026, 10, 1), 49), NOW)
    assert (m, q) == (date(2026, 11, 1), 0)


def test_halt_picks_latest_resume_and_respects_include_queries():
    snap = usage_service.Snapshot(
        session_tokens=250_000, session_resets_at=NOW + timedelta(hours=2),
        day_tokens=1_000_000, day_resets_at=NOW + timedelta(hours=9),
        month_queries=0, month_resets_at=datetime(2026, 12, 1))
    halt = usage_service.compute_halt(plan(), snap)
    assert halt["reason"] == "daily"        # lifts last
    free_snap = usage_service.Snapshot(0, None, 0, None, 50, datetime(2026, 12, 1))
    assert usage_service.compute_halt(FREE, free_snap)["reason"] == "monthly_queries"
    assert usage_service.compute_halt(FREE, free_snap, include_queries=False) is None


def test_unlimited_plan_never_halts():
    ent = plan("enterprise", daily_token_limit=None, session_token_limit=None)
    snap = usage_service.Snapshot(10**9, NOW, 10**9, NOW, 0, NOW)
    assert usage_service.compute_halt(ent, snap) is None


# ───────────────────────── entitlement ─────────────────────────

def test_entitlement_rules():
    f = billing_service._is_entitled
    assert f("active", NOW + timedelta(days=1), False, NOW)
    assert f("active", NOW - timedelta(days=1), False, NOW)            # inside 2-day renewal grace
    assert not f("active", NOW - timedelta(days=3), False, NOW)
    assert f("cancelled", NOW + timedelta(days=5), True, NOW)          # cancel at cycle end keeps access
    assert not f("cancelled", NOW + timedelta(days=5), False, NOW)     # superseded/immediate cancel
    assert not f("halted", NOW + timedelta(days=5), False, NOW)


def test_entitlement_picks_best_and_falls_back_to_free(monkeypatch):
    monkeypatch.setattr(billing_service, "utcnow", lambda: NOW)
    plans = {1: FREE, 2: plan("individual"), 3: plan("maxx", id=3, price=49.99)}
    monkeypatch.setattr(billing_service, "get_plan_by_id", lambda i: plans.get(i))
    monkeypatch.setattr(billing_service, "get_plan_by_code", lambda c: FREE if c == "free" else None)
    rows = [[10, 2, "active", 1, NOW + timedelta(days=9), 0, None, 42],
            [11, 3, "active", 1, NOW + timedelta(days=9), 0, None, 42]]
    monkeypatch.setattr(db, "fetch", lambda q, p=None: rows)
    assert billing_service.get_entitlement(42).plan.code == "maxx"
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [])
    assert billing_service.get_entitlement(42).plan.code == "free"


# ───────────────────────── record_usage SQL values ─────────────────────────

class FakeCursor:
    def __init__(self, row):
        self.row, self.calls = row, []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


def fake_tx(cursor):
    from contextlib import contextmanager

    @contextmanager
    def tx():
        yield cursor
    return tx


def test_record_usage_opens_windows_and_counts(monkeypatch):
    monkeypatch.setattr(usage_service, "utcnow", lambda: NOW)
    cur = FakeCursor((None, 0, None, 0, None, 0))
    monkeypatch.setattr(db, "transaction", fake_tx(cur))
    c = UsageCollector(input_tokens=1000, output_tokens=500, llm_calls=2, count_query=True)
    usage_service.record_usage(42, "transform", c, "t1")
    update = next(p for s, p in cur.calls if s.startswith("UPDATE usage_counters"))
    assert update[:6] == (NOW, 1500, NOW, 1500, date(2026, 11, 1), 1)
    ledger = next(p for s, p in cur.calls if s.startswith("INSERT INTO usage_events"))
    assert ledger[:7] == (42, "transform", 1000, 500, 1500, 2, 1)


def test_record_usage_accumulates_inside_open_window(monkeypatch):
    monkeypatch.setattr(usage_service, "utcnow", lambda: NOW)
    started = NOW - timedelta(hours=1)
    cur = FakeCursor((started, 100, started, 100, date(2026, 11, 1), 4))
    monkeypatch.setattr(db, "transaction", fake_tx(cur))
    usage_service.record_usage(42, "x", UsageCollector(input_tokens=10, output_tokens=5), None)
    update = next(p for s, p in cur.calls if s.startswith("UPDATE usage_counters"))
    assert update[:6] == (started, 115, started, 115, date(2026, 11, 1), 4)   # window start preserved, no query counted


def test_metered_call_records_on_failure_without_counting(monkeypatch):
    seen = []
    monkeypatch.setattr(usage_service, "record_usage", lambda uid, kind, c, t=None: seen.append((uid, kind, c.total, c.count_query)))
    with pytest.raises(HTTPException):
        with usage_service.metered_call(7, "transform"):
            record_llm_usage(100, 50)
            raise HTTPException(status_code=422, detail="bad sql")
    assert seen == [(7, "transform", 150, False)]        # tokens charged, query not counted


def test_metered_call_skips_write_when_nothing_used(monkeypatch):
    seen = []
    monkeypatch.setattr(usage_service, "record_usage", lambda *a, **k: seen.append(1))
    with usage_service.metered_call(7, "dashboard_data"):
        pass
    assert seen == []


def test_threadpool_workers_inherit_collector():
    context_propagation.install()
    c = UsageCollector()
    token = set_collector(c)
    try:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(lambda _: record_llm_usage(10, 5), range(8)))
    finally:
        reset_collector(token)
    assert (c.input_tokens, c.output_tokens, c.llm_calls) == (80, 40, 8)


# ───────────────────────── llm_client metering ─────────────────────────

def _completion(content="ok", usage=(11, 7)):
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=u)


def test_llm_client_reports_provider_usage_and_estimates_when_missing(monkeypatch):
    from services import llm_client
    for comp, expected in ((_completion(usage=(11, 7)), (11, 7)),
                           (_completion("abcdefgh", usage=None), (len("sysuser") // 4, 2))):
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: comp)))
        monkeypatch.setattr(llm_client, "_get_openrouter_client", lambda: fake)
        c = UsageCollector()
        t = set_collector(c)
        try:
            assert llm_client._ask_llm_openrouter("sys", "user")
        finally:
            reset_collector(t)
        assert (c.input_tokens, c.output_tokens) == expected


def test_failed_groq_attempt_is_not_charged(monkeypatch):
    from services import llm_client
    import groq
    err = groq.APIStatusError("rate", response=SimpleNamespace(status_code=429, request=None, headers={}), body=None)
    err.status_code = 429
    def boom(**k): raise err
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    monkeypatch.setattr(llm_client, "_get_groq_client", lambda: fake)
    monkeypatch.setattr(llm_client, "_get_openrouter_client",
                        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **k: _completion(usage=(3, 2))))))
    monkeypatch.setattr(llm_client, "DEFAULT_LLM_PROVIDER", "groq")
    c = UsageCollector()
    t = set_collector(c)
    try:
        llm_client.ask_llm("s", "u")
    finally:
        reset_collector(t)
    assert (c.input_tokens, c.output_tokens, c.llm_calls) == (3, 2, 1)   # only the successful fallback call


# ───────────────────────── razorpay signatures ─────────────────────────

def test_payment_signature():
    good = hmac.new(b"key_secret", b"pay_1|sub_1", hashlib.sha256).hexdigest()
    assert razorpay_service.verify_payment_signature("pay_1", "sub_1", good)
    assert not razorpay_service.verify_payment_signature("pay_1", "sub_2", good)
    assert not razorpay_service.verify_payment_signature("pay_1", "sub_1", "")


def test_webhook_signature():
    body = b'{"event":"x"}'
    good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert razorpay_service.verify_webhook_signature(body, good)
    assert not razorpay_service.verify_webhook_signature(body + b" ", good)


# ───────────────────────── HTTP wiring (app imports, routes, gating) ─────────────────────────

@pytest.fixture()
def client():
    import main
    return TestClient(main.app)


def test_app_mounts_all_routes(client):
    paths = set(client.get('/openapi.json').json()['paths'])
    for p in ("/billing/plans", "/billing/me", "/billing/webhook", "/billing/subscriptions",
              "/billing/subscriptions/verify", "/billing/subscriptions/cancel", "/org/me", "/org/members",
              "/transform", "/upload", "/dashboard_intent_basic"):
        assert p in paths, p


def test_webhook_rejects_bad_signature_and_accepts_good(client, monkeypatch):
    from routers import billing
    body = json.dumps({"event": "subscription.activated", "payload": {}}).encode()
    r = client.post("/billing/webhook", content=body, headers={"x-razorpay-signature": "nope", "x-razorpay-event-id": "e1"})
    assert r.status_code == 400
    processed = []
    monkeypatch.setattr(billing, "_process_event", lambda eid, ev, raw: processed.append(eid))
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    r = client.post("/billing/webhook", content=body, headers={"x-razorpay-signature": sig, "x-razorpay-event-id": "e1"})
    assert r.status_code == 200 and processed == ["e1"]
    r = client.post("/billing/webhook", content=body, headers={"x-razorpay-signature": sig})
    assert r.status_code == 400                      # missing event id


def test_late_charged_event_cannot_revive_superseded_subscription(monkeypatch):
    from routers import billing
    calls = []
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [[5, 42, "cancelled"]] if "FROM subscriptions" in q else [])
    monkeypatch.setattr(billing, "_sync_from_remote", lambda *a, **k: calls.append("sync"))
    monkeypatch.setattr(billing, "_activate", lambda *a, **k: calls.append("activate"))
    monkeypatch.setattr(db, "run", lambda *a, **k: calls.append("run"))
    billing._handle_event("subscription.charged", {"subscription": {"entity": {"id": "sub_old"}}})
    assert calls == []


def test_webhook_failure_is_not_recorded_so_razorpay_retries(monkeypatch):
    from routers import billing
    inserted = []
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [])
    monkeypatch.setattr(db, "run", lambda q, p=None: inserted.append(q))
    monkeypatch.setattr(billing, "_handle_event", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        billing._process_event("e9", {"event": "subscription.charged", "payload": {}}, "{}")
    assert inserted == []


def test_create_subscription_validates_and_returns_checkout_payload(client, monkeypatch):
    from routers import billing
    plans = {"individual": plan("individual"), "enterprise": plan("enterprise", id=4, min_seats=5, price=40)}
    monkeypatch.setattr(billing.billing_service, "get_plan_by_code", lambda c: plans.get(c))
    monkeypatch.setattr(billing.billing_service, "get_entitlement", lambda uid: Entitlement(plan=FREE))
    monkeypatch.setattr(billing.razorpay_service, "create_subscription", lambda p, q, notes: {"id": f"sub_{p}_{q}"})
    runs = []
    monkeypatch.setattr(db, "run", lambda q, p=None: runs.append(p))

    assert client.post("/billing/subscriptions", json={"plan_code": "free"}).status_code == 400
    assert client.post("/billing/subscriptions", json={"plan_code": "enterprise", "seats": 3}).status_code == 400
    r = client.post("/billing/subscriptions", json={"plan_code": "enterprise", "seats": 6})
    assert r.status_code == 200 and r.json() == {"subscription_id": "sub_plan_ent_6", "key_id": "rzp_test_abc",
                                                  "plan_code": "enterprise", "seats": 6}
    r = client.post("/billing/subscriptions", json={"plan_code": "individual", "seats": 9})
    assert r.json()["seats"] == 1                    # single-user plans ignore seats
    assert runs[0][1:3] == (4, 6) or runs[0][1] == 4


def test_verify_rejects_bad_signature_and_foreign_subscription(client, monkeypatch):
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [])
    body = {"razorpay_payment_id": "p", "razorpay_subscription_id": "s", "razorpay_signature": "x"}
    assert client.post("/billing/subscriptions/verify", json=body).status_code == 404
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [[9]])
    assert client.post("/billing/subscriptions/verify", json=body).status_code == 400


def test_transform_is_blocked_with_429_before_any_work(client, monkeypatch):
    from routers import transform
    ran = []
    monkeypatch.setattr(transform, "_transform_impl", lambda *a: ran.append(1))
    monkeypatch.setattr(usage_service.billing_service, "get_entitlement", lambda uid: Entitlement(plan=plan()))
    monkeypatch.setattr(usage_service, "snapshot", lambda uid: usage_service.Snapshot(
        250_000, billing_service.utcnow() + timedelta(hours=2), 250_000, billing_service.utcnow() + timedelta(hours=2), 0, datetime(2099, 1, 1)))
    r = client.post("/transform", json={"userId": 42, "prompt": "x", "tables": [1], "sessionId": 1})
    assert r.status_code == 429 and ran == []
    d = r.json()["detail"]
    assert d["code"] == "USAGE_LIMIT" and d["reason"] == "session" and d["resumes_at"].endswith("Z")


def test_transform_success_counts_query_and_blocks_other_user(client, monkeypatch):
    from routers import transform
    recorded = []
    monkeypatch.setattr(usage_service.billing_service, "get_entitlement", lambda uid: Entitlement(plan=FREE))
    monkeypatch.setattr(usage_service, "snapshot", lambda uid: usage_service.Snapshot(0, None, 0, None, 0, datetime(2099, 1, 1)))
    monkeypatch.setattr(usage_service, "record_usage", lambda uid, kind, c, t=None: recorded.append((uid, kind, c.total, c.count_query)))
    def impl(payload, trace):
        record_llm_usage(1000, 200)
        return {"ok": True}
    monkeypatch.setattr(transform, "_transform_impl", impl)
    ok = client.post("/transform", json={"userId": 42, "prompt": "x", "tables": [1], "sessionId": 1})
    assert ok.status_code == 200 and recorded == [(42, "transform", 1200, True)]
    assert client.post("/transform", json={"userId": 99, "prompt": "x", "tables": [1], "sessionId": 1}).status_code == 401


def test_upload_enforces_file_cap(client, monkeypatch):
    monkeypatch.setattr(usage_service.billing_service, "get_entitlement", lambda uid: Entitlement(plan=FREE))
    monkeypatch.setattr(usage_service, "files_used", lambda uid: 5)
    r = client.post("/upload?userId=42", files={"file": ("a.csv", b"a,b\n1,2")})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FILE_LIMIT"


def test_cors_allows_localhost_with_credentials(client):
    r = client.options("/billing/plans", headers={"Origin": "http://localhost:51234",
                                                   "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:51234"
    assert r.headers.get("access-control-allow-credentials") == "true"


# ───────────────────────── retention ─────────────────────────

def test_retention_hides_only_expired_and_skips_recent_downgrades(monkeypatch):
    monkeypatch.setattr(billing_service, "utcnow", lambda: NOW)
    hidden, ents = [], {1: FREE, 2: FREE, 3: plan("maxx", retention_days=None)}
    monkeypatch.setattr(retention.billing_service, "get_entitlement", lambda uid: Entitlement(plan=ents[uid]))
    def fetch(q, p=None):
        if "SELECT DISTINCT user_id" in q: return [[1], [2], [3]]
        if "SELECT id FROM table_info" in q: return [[100 + p[0]]]
        return []
    monkeypatch.setattr(db, "fetch", fetch)
    monkeypatch.setattr(db, "get_single_value", lambda q, p=None:
                        90 if "MIN(retention_days)" in q else (NOW - timedelta(days=3) if p == (2,) else None))
    monkeypatch.setattr(db, "transaction", fake_tx(FakeCursor(None)))
    monkeypatch.setattr(retention.dataset_cleanup, "soft_delete_dataset", lambda cur, tid: hidden.append(tid), raising=False)
    out = retention.run_retention_sweep()
    assert hidden == [101]            # user1 (free, long-time) hidden; user2 recently lapsed; user3 forever
    assert out["hidden"] == 1


def test_purge_marks_only_dropped_tables(monkeypatch):
    runs = []
    monkeypatch.setattr(db, "fetch", lambda q, p=None: [[1, "t1"], [2, "t2"]])
    monkeypatch.setattr(db, "run", lambda q, p=None: runs.append(p))
    monkeypatch.setattr(retention.dataset_cleanup, "drop_physical_table_if_unused", lambda i, n: i == 1, raising=False)
    assert retention.run_purge_sweep() == {"candidates": 2, "purged": 1}
    assert runs == [(1,)]


def test_timestamp_helpers_are_naive_utc():
    assert razorpay_service.ts_to_dt(0) is None
    assert razorpay_service.ts_to_dt(1_700_000_000) == datetime(2023, 11, 14, 22, 13, 20)
    assert billing_service.utcnow().tzinfo is None
