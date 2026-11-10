"""
Test harness. Stubs ONLY the modules that are not part of the billing change
(auth/token plumbing, AI trace, pipeline, ingest, other routers) so the billing,
metering, retention and wiring code can be exercised without a SQL Server.
Run from the project root:  pytest tests -q
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update({
    "DB_HOST": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_DATABASE": "x", "DB_PORT": "1433",
    "JWT_SECRET_KEY": "test-secret", "ENV": "development", "RUN_SCHEDULER": "0",
    "RAZORPAY_KEY_ID": "rzp_test_abc", "RAZORPAY_KEY_SECRET": "key_secret",
    "RAZORPAY_WEBHOOK_SECRET": "whsec", "RAZORPAY_PLAN_INDIVIDUAL": "plan_ind",
    "RAZORPAY_PLAN_MAXX": "plan_maxx", "RAZORPAY_PLAN_ENTERPRISE": "plan_ent",
})

from fastapi import APIRouter  # noqa: E402
from pydantic import BaseModel  # noqa: E402

AUTHED_USER = {"id": 42}


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


def _install_stubs():
    def _unpatched(*a, **k):
        raise AssertionError("core.db was called without being patched by the test")

    _stub("core.db", fetch=_unpatched, run=_unpatched, insert=_unpatched, transaction=_unpatched,
          get_single_value=_unpatched, get_connection=_unpatched, fetch_with_columns=_unpatched)
    import core
    core.db = sys.modules["core.db"]

    def get_current_user_id():
        return AUTHED_USER["id"]

    def require_same_user(a, b):
        if a != b:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="User Mismatch")

    _stub("core.security", get_current_user_id=get_current_user_id, require_same_user=require_same_user)
    _stub("core.ai_trace", new_trace_id=lambda: "trace-1", log_ai_event=lambda *a, **k: None)
    _stub("core.sql_safety", classify_sql_error=lambda e: {"code": "UNKNOWN", "retryable": False},
          sanitize_sql=lambda s: s)
    for name in ("pipeline", "query_generation", "table_ingest", "email_service", "token_service",
                 "dataset_cleanup"):
        _stub(f"services.{name}")
    sys.modules["services.table_ingest"].ingest_table_and_session = None
    sys.modules["services.dashboard"] = types.ModuleType("services.dashboard")

    class TransformPayload(BaseModel):
        userId: int
        prompt: str
        tables: list[int]
        sessionId: int | None = None

    _stub("models", TransformPayload=TransformPayload)
    for name in ("auth", "sessions", "merge", "public", "charts", "query", "data_sources"):
        _stub(f"routers.{name}", router=APIRouter())


_install_stubs()
