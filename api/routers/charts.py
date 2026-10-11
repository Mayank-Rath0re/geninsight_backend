# routers/charts.py
"""
CRUD for `charts` (session-scoped saved visualizations) and
`dashboard_charts` (which charts belong to which dashboard).

Both tables exist in init.sql with no prior endpoints at all — this
router fills that gap. Chart creation here persists a caller-supplied
spec (chart_type/prompt/sql_query/metadata) rather than generating SQL
via the LLM; no chart-SQL-generation service exists elsewhere in this
codebase to reuse, and inventing one is out of scope for a CRUD pass.
If AI-generated chart SQL becomes a real feature, add a
services/chart_generation.py analogous to services/query_generation.py
and call it from /create_chart before the INSERT below.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db
from core.security import get_current_user_id, require_same_user

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["charts"])


def _check_session_owner(session_id: int, authed_user_id: int) -> None:
    owner = db.get_single_value("SELECT user_id FROM sessions WHERE session_id = %s", (session_id,))
    if owner is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    if owner != authed_user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")


def _check_chart_owner(chart_id: int, authed_user_id: int) -> int:
    """Returns the chart's session_id if the caller owns it, else raises."""
    row = db.fetch(
        """
        SELECT c.session_id, s.user_id
        FROM charts c JOIN sessions s ON s.session_id = c.session_id
        WHERE c.chart_id = %s AND c.is_deleted = 0
        """,
        params=(chart_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Chart not found")
    session_id, owner = row[0]
    if owner != authed_user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")
    return session_id


def _check_dashboard_owner(dashboard_id: int, authed_user_id: int) -> None:
    owner = db.get_single_value("SELECT user_id FROM dashboards WHERE dashboard_id = %s", (dashboard_id,))
    if owner is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    if owner != authed_user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")


# ─────────────────────────────────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────────────────────────────────

class CreateChartRequest(BaseModel):
    userId: int
    session_id: int
    chart_type: str
    prompt: str
    sql_query: str
    metadata: dict = {}


@router.post("/create_chart")
def create_chart(payload: CreateChartRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    _check_session_owner(payload.session_id, authed_user_id)

    chart_id = db.insert(
        "INSERT INTO charts (session_id, chart_type, prompt, sql_query, metadata) "
        "OUTPUT INSERTED.chart_id VALUES (%s, %s, %s, %s, %s)",
        (payload.session_id, payload.chart_type, payload.prompt, payload.sql_query, json.dumps(payload.metadata)),
    )
    if chart_id == -1:
        raise HTTPException(status_code=400, detail="Failed to create chart.")
    return {"chart_id": chart_id, "session_id": payload.session_id}


@router.get("/session_charts")
def get_session_charts(session_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    _check_session_owner(session_id, authed_user_id)

    rows = db.fetch(
        "SELECT chart_id, chart_type, prompt, sql_query, metadata, created_at "
        "FROM charts WHERE session_id = %s AND is_deleted = 0 ORDER BY created_at ASC",
        params=(session_id,),
    )
    return {
        "charts": [
            {
                "chart_id": r[0],
                "chart_type": r[1],
                "prompt": r[2],
                "sql_query": r[3],
                "metadata": json.loads(r[4]) if r[4] else {},
                "created_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    }


@router.post("/delete_chart")
def delete_chart(chart_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    _check_chart_owner(chart_id, authed_user_id)
    db.run("UPDATE charts SET is_deleted = 1, deleted_at = SYSDATETIME() WHERE chart_id = %s", (chart_id,))
    return {"chart_id": chart_id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────
# DASHBOARD <-> CHART LINKS
# ─────────────────────────────────────────────────────────────────────────

class AttachChartRequest(BaseModel):
    userId: int
    dashboard_id: int
    chart_id: int


@router.post("/attach_chart_to_dashboard")
def attach_chart_to_dashboard(payload: AttachChartRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    _check_dashboard_owner(payload.dashboard_id, authed_user_id)
    _check_chart_owner(payload.chart_id, authed_user_id)

    existing = db.fetch(
        "SELECT 1 FROM dashboard_charts WHERE dashboard_id = %s AND chart_id = %s",
        params=(payload.dashboard_id, payload.chart_id),
    )
    if existing:
        return {"dashboard_id": payload.dashboard_id, "chart_id": payload.chart_id, "already_attached": True}

    db.run(
        "INSERT INTO dashboard_charts (dashboard_id, chart_id) VALUES (%s, %s)",
        (payload.dashboard_id, payload.chart_id),
    )
    return {"dashboard_id": payload.dashboard_id, "chart_id": payload.chart_id, "already_attached": False}


@router.post("/detach_chart_from_dashboard")
def detach_chart_from_dashboard(payload: AttachChartRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    _check_dashboard_owner(payload.dashboard_id, authed_user_id)

    db.run(
        "DELETE FROM dashboard_charts WHERE dashboard_id = %s AND chart_id = %s",
        (payload.dashboard_id, payload.chart_id),
    )
    return {"dashboard_id": payload.dashboard_id, "chart_id": payload.chart_id, "detached": True}


@router.get("/dashboard_charts")
def get_dashboard_charts(dashboard_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    _check_dashboard_owner(dashboard_id, authed_user_id)

    rows = db.fetch(
        """
        SELECT c.chart_id, c.chart_type, c.prompt, c.sql_query, c.metadata
        FROM dashboard_charts dc
        JOIN charts c ON c.chart_id = dc.chart_id
        WHERE dc.dashboard_id = %s AND c.is_deleted = 0
        """,
        params=(dashboard_id,),
    )
    return {
        "charts": [
            {
                "chart_id": r[0],
                "chart_type": r[1],
                "prompt": r[2],
                "sql_query": r[3],
                "metadata": json.loads(r[4]) if r[4] else {},
            }
            for r in rows
        ]
    }
