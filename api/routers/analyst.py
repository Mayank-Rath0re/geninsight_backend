# routers/analyst.py
"""
AI Analyst endpoints.

POST /ai_analyst              — run the 5-step trend analysis for one table.
                                The only input is the uploaded table id; ownership
                                comes from the auth token. Without `force`, an
                                existing saved report is returned as-is (no AI).
GET  /analyst_report          — load one saved report (no AI).
GET  /user_analyst_reports    — list saved reports (no AI).
POST /delete_analyst_report   — soft-delete a saved report.
POST /share_analyst_report    — enable/disable/refresh a public snapshot.
POST /ai_analyst/export_pdf   — lay out an already-computed report as a PDF.
"""

import json
import logging
import re
import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from core import db
from core.ai_trace import log_ai_event, new_trace_id
from core.security import get_current_user_id
from services import analyst as analyst_service
from services import analyst_pdf
from services import usage_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["analyst"])

# Billing notes
#  - Loading, listing or sharing a saved report never calls the AI.
#  - Only a forced run (or the first run for a table) reaches check_quota and
#    is metered.
#  - A run counts toward the Free monthly query cap only when it used the AI
#    and succeeded (meter.llm_calls > 0).


def _iso(value):
    return value.isoformat() if value else None


def _parse_payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _share_state(report_id: int) -> dict:
    rows = db.fetch(
        "SELECT is_shared, share_token, shared_at, updated_at FROM analyst_reports "
        "WHERE report_id = %s",
        params=(report_id,),
    )
    if not rows:
        return {"is_shared": False, "share_token": None, "shared_at": None,
                "snapshot_stale": False, "updated_at": None}
    shared, token, shared_at, updated_at = rows[0]
    is_shared = bool(shared)
    stale = bool(
        is_shared and (shared_at is None or (updated_at is not None and updated_at > shared_at))
    )
    return {
        "is_shared": is_shared,
        "share_token": token if is_shared else None,
        "shared_at": _iso(shared_at),
        "snapshot_stale": stale,
        "updated_at": _iso(updated_at),
    }


def _check_report_owner(report_id: int, user_id: int) -> None:
    owner = db.get_single_value(
        "SELECT user_id FROM analyst_reports WHERE report_id = %s AND is_deleted = 0",
        (report_id,),
    )
    if owner is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_NOT_FOUND",
                    "message": "That report no longer exists.", "retryable": False},
        )
    if owner != user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to that report.", "retryable": False},
        )


def _report_response(payload: dict, report_id: int, reused: bool) -> dict:
    out = dict(payload)
    out["report_id"] = report_id
    out["reused"] = reused
    out.update(_share_state(report_id))
    return out


def _latest_saved_report(user_id: int, table_id: int):
    rows = db.fetch(
        "SELECT TOP 1 report_id, payload FROM analyst_reports "
        "WHERE user_id = %s AND table_id = %s AND is_deleted = 0 "
        "ORDER BY updated_at DESC, report_id DESC",
        params=(user_id, table_id),
    )
    if not rows:
        return None
    return rows[0][0], _parse_payload(rows[0][1])


def _save_report(user_id: int, table_id: int, result: dict, title: str):
    """One saved report per user/table: a re-run replaces the previous payload."""
    try:
        existing = db.get_single_value(
            "SELECT report_id FROM analyst_reports "
            "WHERE user_id = %s AND table_id = %s AND is_deleted = 0 "
            "ORDER BY updated_at DESC, report_id DESC",
            (user_id, table_id),
        )
        if existing:
            db.run(
                "UPDATE analyst_reports SET payload = %s, title = %s, "
                "updated_at = SYSDATETIME() WHERE report_id = %s",
                (json.dumps(result, default=str), title[:200], existing),
            )
            return existing
        return db.insert(
            "INSERT INTO analyst_reports (user_id, table_id, title, payload) "
            "OUTPUT INSERTED.report_id VALUES (%s, %s, %s, %s)",
            (user_id, table_id, title[:200], json.dumps(result, default=str)),
        )
    except Exception:
        # Persistence is a convenience: a failed save must never lose the run.
        logger.exception("Could not save AI Analyst report for table %s", table_id)
        return None


# ─────────────────────────────────────────────────────────────────────────
# RUN / REUSE
# ─────────────────────────────────────────────────────────────────────────

class AnalystRequest(BaseModel):
    table_id: int
    force: bool = False


@router.post("/ai_analyst")
def run_ai_analyst(payload: AnalystRequest, authed_user_id: int = Depends(get_current_user_id)):
    table_id = payload.table_id
    rows = db.fetch(
        "SELECT name, metadata, knowledgebase, user_id FROM table_info "
        "WHERE id = %s AND is_deleted = 0",
        params=(table_id,),
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail={"code": "TABLE_NOT_FOUND",
                    "message": "That table no longer exists.", "retryable": False},
        )
    table_name, metadata_raw, knowledge_base_raw, owner = rows[0]
    if owner is not None and owner != authed_user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to that table.", "retryable": False},
        )

    if not payload.force:
        saved = _latest_saved_report(authed_user_id, table_id)
        if saved and saved[1].get("spec"):
            log_ai_event(new_trace_id(), "analyst.reused",
                         report_id=saved[0], table_id=table_id)
            return _report_response(saved[1], saved[0], reused=True)

    metadata = metadata_raw
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    if not isinstance(metadata, dict) or not metadata:
        raise HTTPException(
            status_code=422,
            detail={"code": "TABLE_NOT_READY",
                    "message": "This table is still being prepared. Try again in a moment.",
                    "retryable": True},
        )

    knowledge_base = knowledge_base_raw
    if isinstance(knowledge_base, str):
        try:
            knowledge_base = json.loads(knowledge_base)
        except ValueError:
            pass

    trace_id = new_trace_id()
    usage_service.check_quota(authed_user_id)
    with usage_service.metered_call(authed_user_id, "ai_analyst", trace_id) as meter:
        try:
            log_ai_event(trace_id, "analyst.request", table_id=table_id,
                         user_id=authed_user_id, force=payload.force)
            result = analyst_service.build_analysis(
                table_id, table_name, metadata, knowledge_base, trace_id
            )
            meter.count_query = meter.llm_calls > 0
            report_id = _save_report(
                authed_user_id, table_id, result, result.get("analysis_title") or table_name
            )
            if report_id:
                return _report_response(result, report_id, reused=False)
            return {**result, "report_id": None, "reused": False}
        except ValueError as exc:
            log_ai_event(trace_id, "analyst.no_data", level="warn", error=str(exc))
            raise HTTPException(
                status_code=422,
                detail={"code": "ANALYSIS_NO_DATA",
                        "message": "I couldn't compute any analysis from this table. "
                                   "Try transforming the data first, then run AI Analyst again.",
                        "retryable": True, "trace_id": trace_id},
            )
        except HTTPException:
            raise
        except Exception:
            logger.exception("AI Analyst failed for table %s (trace %s)", table_id, trace_id)
            log_ai_event(trace_id, "analyst.failed", level="error")
            raise HTTPException(
                status_code=500,
                detail={"code": "ANALYST_FAILED",
                        "message": "The analysis couldn't be completed. Please try again.",
                        "retryable": True, "trace_id": trace_id},
            )


# ─────────────────────────────────────────────────────────────────────────
# READ / LIST / DELETE (no AI)
# ─────────────────────────────────────────────────────────────────────────

@router.get("/analyst_report")
def get_analyst_report(report_id: int, userId: int,
                       authed_user_id: int = Depends(get_current_user_id)):
    if authed_user_id != userId:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to that report.", "retryable": False},
        )
    _check_report_owner(report_id, userId)

    rows = db.fetch(
        "SELECT payload FROM analyst_reports WHERE report_id = %s AND is_deleted = 0",
        params=(report_id,),
    )
    payload = _parse_payload(rows[0][0]) if rows else {}
    if not payload.get("spec"):
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_NOT_FOUND",
                    "message": "That report no longer exists.", "retryable": False},
        )
    return _report_response(payload, report_id, reused=True)


@router.get("/user_analyst_reports")
def get_user_analyst_reports(userId: int, table_id: int | None = None,
                             authed_user_id: int = Depends(get_current_user_id)):
    if authed_user_id != userId:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to those reports.", "retryable": False},
        )
    sql = (
        "SELECT r.report_id, r.table_id, t.name, r.title, r.payload, r.is_shared, "
        "r.share_token, r.shared_at, r.updated_at "
        "FROM analyst_reports r JOIN table_info t ON t.id = r.table_id "
        "WHERE r.user_id = %s AND r.is_deleted = 0"
    )
    params: list = [userId]
    if table_id is not None:
        sql += " AND r.table_id = %s"
        params.append(table_id)
    sql += " ORDER BY r.updated_at DESC, r.report_id DESC"
    rows = db.fetch(sql, params=tuple(params))

    out = []
    for r in rows:
        payload = _parse_payload(r[4])
        out.append({
            "report_id": r[0],
            "table_id": r[1],
            "table_name": r[2],
            "title": r[3] or payload.get("analysis_title") or r[2],
            "is_shared": bool(r[5]),
            "share_token": r[6] if r[5] else None,
            "shared_at": _iso(r[7]),
            "updated_at": _iso(r[8]),
        })
    return {"reports": out}


class DeleteAnalystReportRequest(BaseModel):
    userId: int
    report_id: int


@router.post("/delete_analyst_report")
def delete_analyst_report(payload: DeleteAnalystReportRequest,
                          authed_user_id: int = Depends(get_current_user_id)):
    if authed_user_id != payload.userId:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to that report.", "retryable": False},
        )
    _check_report_owner(payload.report_id, payload.userId)

    db.run(
        "UPDATE analyst_reports SET is_deleted = 1, deleted_at = SYSDATETIME(), "
        "is_shared = 0, share_token = NULL, shared_snapshot = NULL, shared_at = NULL "
        "WHERE report_id = %s",
        (payload.report_id,),
    )
    return {"report_id": payload.report_id, "deleted": True}


# ─────────────────────────────────────────────────────────────────────────
# SHARE (no AI)
# ─────────────────────────────────────────────────────────────────────────

class ShareAnalystReportRequest(BaseModel):
    userId: int
    report_id: int
    enabled: bool = True
    refresh: bool = False


@router.post("/share_analyst_report")
def share_analyst_report(payload: ShareAnalystReportRequest,
                         authed_user_id: int = Depends(get_current_user_id)):
    if authed_user_id != payload.userId:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN",
                    "message": "You don't have access to that report.", "retryable": False},
        )
    trace_id = new_trace_id()
    try:
        _check_report_owner(payload.report_id, payload.userId)

        if not payload.enabled:
            db.run(
                "UPDATE analyst_reports SET is_shared = 0, share_token = NULL, "
                "shared_snapshot = NULL, shared_at = NULL WHERE report_id = %s",
                (payload.report_id,),
            )
            log_ai_event(trace_id, "analyst.share_disabled", report_id=payload.report_id)
            return {"report_id": payload.report_id, **_share_state(payload.report_id)}

        row = db.fetch(
            "SELECT payload, is_shared, share_token FROM analyst_reports "
            "WHERE report_id = %s AND is_deleted = 0",
            params=(payload.report_id,),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "REPORT_NOT_FOUND",
                        "message": "That report no longer exists.", "retryable": False},
            )
        result, is_shared, token = _parse_payload(row[0][0]), row[0][1], row[0][2]

        if is_shared and token and not payload.refresh:
            return {"report_id": payload.report_id, **_share_state(payload.report_id)}

        snapshot = analyst_service.public_snapshot(result)
        if not snapshot["data"]:
            raise HTTPException(
                status_code=422,
                detail={"code": "NOTHING_TO_SHARE",
                        "message": "None of this report's sections could load, so there is "
                                   "nothing to share yet.",
                        "retryable": False, "trace_id": trace_id},
            )

        token = token or secrets.token_urlsafe(24)
        db.run(
            "UPDATE analyst_reports SET is_shared = 1, share_token = %s, shared_snapshot = %s, "
            "shared_at = SYSDATETIME() WHERE report_id = %s",
            (token, json.dumps(snapshot, default=str), payload.report_id),
        )
        log_ai_event(trace_id, "analyst.share_enabled", report_id=payload.report_id,
                     refreshed=payload.refresh)
        return {"report_id": payload.report_id, **_share_state(payload.report_id)}

    except HTTPException:
        raise
    except Exception as exc:
        log_ai_event(trace_id, "analyst.share_failed", level="error", error=str(exc),
                     report_id=payload.report_id)
        logger.exception("Share failed for report %s (trace %s)", payload.report_id, trace_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "SHARE_FAILED",
                    "message": "I couldn't update sharing for this report. Please try again.",
                    "retryable": True, "trace_id": trace_id},
        )


# ─────────────────────────────────────────────────────────────────────────
# PDF (no AI)
# ─────────────────────────────────────────────────────────────────────────

class AnalystPdfRequest(BaseModel):
    analysis_title: str = "AI Analyst report"
    analysis_goal: str = ""
    table_name: str = ""
    narrative_summary: str = ""
    kpis: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    data: Dict[str, Any] = {}
    # Step id -> "data:image/png;base64,..." captured from the live charts.
    chart_images: Dict[str, str] = {}


def _safe_filename(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip().replace(" ", "_")
    slug = re.sub(r"_+", "_", slug)[:60].strip("._")
    return (slug or "ai_analyst_report") + ".pdf"


@router.post("/ai_analyst/export_pdf")
def export_analyst_pdf(payload: AnalystPdfRequest, authed_user_id: int = Depends(get_current_user_id)):
    # Rendering takes the client's computed rows; keep the payload bounded so a
    # malformed request can't make the server allocate unbounded memory.
    if len(payload.steps) > 10 or len(payload.kpis) > 10:
        raise HTTPException(status_code=422, detail="Too many report sections.")
    total_rows = sum(
        len((value or {}).get("rows") or [])
        for value in payload.data.values()
        if isinstance(value, dict)
    )
    if total_rows > 500:
        raise HTTPException(status_code=413, detail="Report is too large to export.")
    if len(payload.chart_images) > 10:
        raise HTTPException(status_code=422, detail="Too many chart images.")
    if sum(len(v or "") for v in payload.chart_images.values()) > 12_000_000:
        raise HTTPException(status_code=413, detail="Chart images are too large.")

    try:
        pdf = analyst_pdf.render_analysis_pdf(payload.model_dump())
    except Exception:
        logger.exception("PDF export failed for user %s", authed_user_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "PDF_FAILED",
                    "message": "The PDF couldn't be generated. Please try again.",
                    "retryable": True},
        )

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(payload.analysis_title)}"'},
    )
