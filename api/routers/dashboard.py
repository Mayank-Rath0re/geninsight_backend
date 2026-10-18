import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db
from core.ai_trace import log_ai_event, new_trace_id
from core.security import get_current_user_id, require_same_user
from services import dashboard as dashboard_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["dashboard"])

def _iso(value):
    return value.isoformat() if value else None

def _load_table_context(table_id: int):
    rows = db.fetch(
        "SELECT name, metadata, knowledgebase FROM table_info WHERE id = %s",
        params=(table_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Table not found.")
    return rows[0][0], rows[0][1], rows[0][2]

def _check_dashboard_owner(dashboard_id: int, user_id: int) -> None:
    owner = db.get_single_value("SELECT user_id FROM dashboards WHERE dashboard_id = %s", (dashboard_id,))
    if owner is None:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    if owner != user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")

def _failed_item_ids(spec: dict) -> list:
    return [
        i.get("id")
        for k in ("kpis", "charts")
        for i in spec.get(k, [])
        if i.get("status") != "ok"
    ]

def _any_ok(spec: dict) -> bool:
    return any(i.get("status") == "ok" for k in ("kpis", "charts") for i in spec.get(k, []))

def _share_state(dashboard_id: int) -> dict:
    rows = db.fetch(
        "SELECT is_shared, share_token, shared_at, updated_at FROM dashboards WHERE dashboard_id = %s",
        params=(dashboard_id,),
    )
    if not rows:
        return {"is_shared": False, "share_token": None, "shared_at": None,
                "snapshot_stale": False, "updated_at": None}
    shared, token, shared_at, updated_at = rows[0]
    is_shared = bool(shared)
    stale = bool(is_shared and (shared_at is None or (updated_at is not None and updated_at > shared_at)))
    return {
        "is_shared": is_shared,
        "share_token": token if is_shared else None,
        "shared_at": _iso(shared_at),
        "snapshot_stale": stale,
        "updated_at": _iso(updated_at),
    }

def _payload(dashboard_id: int, spec: dict, data: dict, trace_id: str, reused: bool = False, summary=None) -> dict:
    out = {
        "dashboard_id": dashboard_id,
        "dashboard_spec": spec,
        "data": data,
        "trace_id": trace_id,
        "reused": reused,
        **_share_state(dashboard_id),
    }
    if summary is not None:
        out["dashboard_intent"] = summary
    return out

class DashboardIntentBasicRequest(BaseModel):
    userId: int
    table_id: int
    prompt: str
    force_regenerate: bool = False

@router.post("/dashboard_intent_basic")
def dashboard_intent_basic(payload: DashboardIntentBasicRequest, authed_user_id: int = Depends(get_current_user_id)):
    userId = payload.userId
    table_id = payload.table_id
    prompt = payload.prompt
    require_same_user(authed_user_id, userId)

    trace_id = new_trace_id()
    try:
        owner = db.get_single_value("SELECT user_id FROM table_info WHERE id = %s", (table_id,))
        if owner is None:
            raise HTTPException(status_code=404, detail="Table not found.")
        if owner != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        table_name, metadata_raw, kb_raw = _load_table_context(table_id)
        metadata = json.loads(metadata_raw)
        knowledge_base = json.loads(kb_raw)

        log_ai_event(trace_id, "dashboard.intent_request", prompt=prompt, table_id=table_id,
                     user_id=userId, force=payload.force_regenerate)

        existing = db.fetch(
            "SELECT TOP 1 dashboard_id, dashboard_intent FROM dashboards "
            "WHERE user_id = %s AND table_id = %s AND is_deleted = 0 AND dashboard_intent IS NOT NULL "
            "ORDER BY updated_at DESC, dashboard_id DESC",
            params=(userId, table_id),
        )
        existing_id = existing[0][0] if existing else None

        if existing_id and not payload.force_regenerate:
            try:
                saved = json.loads(existing[0][1])
            except ValueError:
                saved = None
            if saved:
                saved, data = dashboard_service.build_dashboard(
                    saved, table_name, metadata, knowledge_base, trace_id, allow_ai=False
                )
                if _any_ok(saved):
                    db.run(
                        "UPDATE dashboards SET dashboard_intent = %s WHERE dashboard_id = %s",
                        (json.dumps(saved), existing_id),
                    )
                    log_ai_event(trace_id, "dashboard.reused", dashboard_id=existing_id,
                                 failed=_failed_item_ids(saved))
                    return _payload(existing_id, saved, data, trace_id, reused=True,
                                    summary=dashboard_service.render_dashboard_summary(saved))
                log_ai_event(trace_id, "dashboard.reuse_unusable", level="warn", dashboard_id=existing_id)

        spec = dashboard_service.extract_dashboard_intent(
            table_id, prompt, knowledge_base, metadata, table_name
        )
        log_ai_event(trace_id, "dashboard.intent_generated", intent=spec)

        spec, data = dashboard_service.build_dashboard(spec, table_name, metadata, knowledge_base, trace_id)

        if existing_id:
            db.run(
                "UPDATE dashboards SET dashboard_intent = %s, user_response = NULL, prompt = %s, "
                "updated_at = SYSDATETIME() WHERE dashboard_id = %s",
                (json.dumps(spec), prompt, existing_id),
            )
            dashboard_id = existing_id
        else:
            dashboard_id = db.insert(
                "INSERT INTO dashboards (user_id, table_id, dashboard_intent, prompt) "
                "OUTPUT INSERTED.dashboard_id VALUES (%s, %s, %s, %s)",
                (userId, table_id, json.dumps(spec), prompt),
            )

        log_ai_event(trace_id, "dashboard.done", dashboard_id=dashboard_id, failed=_failed_item_ids(spec))
        return _payload(dashboard_id, spec, data, trace_id, reused=False,
                        summary=dashboard_service.render_dashboard_summary(spec))

    except HTTPException:
        raise
    except Exception as e:
        log_ai_event(trace_id, "dashboard.intent_failed", level="error", error=str(e), prompt=prompt)
        logger.exception("Dashboard intent failed for table %s (trace %s)", table_id, trace_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DASHBOARD_FAILED",
                "message": "I couldn't build that dashboard. Please try again or rephrase.",
                "retryable": True,
                "trace_id": trace_id,
            },
        )

class UpdateDashboardItemRequest(BaseModel):
    userId: int
    dashboard_id: int
    item_id: str
    prompt: str

@router.post("/update_dashboard_item")
def update_dashboard_item(payload: UpdateDashboardItemRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    trace_id = new_trace_id()
    try:
        _check_dashboard_owner(payload.dashboard_id, payload.userId)
        row = db.fetch(
            "SELECT dashboard_intent, table_id FROM dashboards WHERE dashboard_id = %s AND is_deleted = 0",
            params=(payload.dashboard_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Dashboard not found.")
        intent = json.loads(row[0][0])
        table_name, metadata_raw, kb_raw = _load_table_context(row[0][1])
        metadata, knowledge_base = json.loads(metadata_raw), json.loads(kb_raw)

        if dashboard_service.find_item(intent, payload.item_id) is None:
            raise HTTPException(status_code=404, detail="Widget not found.")

        log_ai_event(trace_id, "dashboard.item_edit_request", dashboard_id=payload.dashboard_id,
                     item_id=payload.item_id, prompt=payload.prompt)

        edit = dashboard_service.edit_dashboard_item(intent, payload.item_id, payload.prompt, metadata, trace_id)

        data = {}
        if edit["action"] == "updated":
            intent, data = dashboard_service.build_dashboard(
                intent, table_name, metadata, knowledge_base, trace_id, only_ids={payload.item_id}
            )
            new_item = dashboard_service.find_item(intent, payload.item_id)[3]
            if new_item.get("status") != "ok":
                log_ai_event(trace_id, "dashboard.item_edit_failed", level="error",
                             item_id=payload.item_id, error_code=new_item.get("error"), item=new_item)
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "ITEM_UPDATE_FAILED",
                        "message": "I couldn't apply that change to this widget. Try rephrasing it.",
                        "retryable": True,
                        "trace_id": trace_id,
                    },
                )

        db.run(
            "UPDATE dashboards SET dashboard_intent = %s, updated_at = SYSDATETIME() WHERE dashboard_id = %s",
            (json.dumps(intent), payload.dashboard_id),
        )
        log_ai_event(trace_id, "dashboard.item_edit_done", item_id=payload.item_id, action=edit["action"])
        out = _payload(payload.dashboard_id, intent, data, trace_id)
        out["updated_item_id"] = payload.item_id
        out["removed"] = edit["action"] == "removed"
        return out

    except HTTPException:
        raise
    except Exception as e:
        log_ai_event(trace_id, "dashboard.item_edit_error", level="error", error=str(e),
                     item_id=payload.item_id, prompt=payload.prompt)
        logger.exception("Item edit failed (trace %s)", trace_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DASHBOARD_UPDATE_FAILED",
                "message": "I couldn't update that widget. Please try again.",
                "retryable": True,
                "trace_id": trace_id,
            },
        )

@router.get("/user_dashboards")
def get_user_dashboards(userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    rows = db.fetch(
        "SELECT d.dashboard_id, d.table_id, t.name, d.dashboard_intent, d.status, d.created_at, "
        "d.updated_at, d.is_shared "
        "FROM dashboards d JOIN table_info t ON t.id = d.table_id "
        "WHERE d.user_id = %s AND d.is_deleted = 0 ORDER BY d.updated_at DESC, d.dashboard_id DESC",
        params=(userId,),
    )
    out = []
    for r in rows:
        title = None
        try:
            title = (json.loads(r[3]) or {}).get("dashboard_title") if r[3] else None
        except ValueError:
            title = None
        out.append({
            "dashboard_id": r[0],
            "table_id": r[1],
            "table_name": r[2],
            "title": title or r[2],
            "status": r[4],
            "created_at": _iso(r[5]),
            "updated_at": _iso(r[6]),
            "is_shared": bool(r[7]),
        })
    return {"dashboards": out}

@router.get("/dashboard")
def get_dashboard(dashboard_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    row = db.fetch(
        "SELECT dashboard_id, table_id, dashboard_intent, user_response, status, breath_time, created_at, user_id "
        "FROM dashboards WHERE dashboard_id = %s AND is_deleted = 0",
        params=(dashboard_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    r = row[0]
    if r[7] != userId:
        raise HTTPException(status_code=401, detail="User Mismatch")
    return {
        "dashboard_id": r[0],
        "table_id": r[1],
        "dashboard_intent": json.loads(r[2]) if r[2] else None,
        "user_response": r[3],
        "status": r[4],
        "breath_time": r[5],
        "created_at": _iso(r[6]),
        **_share_state(dashboard_id),
    }

@router.get("/dashboard_data")
def dashboard_data(dashboard_id: int, userId: int, repair: bool = True,
                   authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    trace_id = new_trace_id()
    try:
        row = db.fetch(
            "SELECT dashboard_intent, table_id, user_id FROM dashboards "
            "WHERE dashboard_id = %s AND is_deleted = 0",
            params=(dashboard_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Dashboard not found.")
        if row[0][2] != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        intent = json.loads(row[0][0])
        table_name, metadata_raw, kb_raw = _load_table_context(row[0][1])
        spec, data = dashboard_service.build_dashboard(
            intent, table_name, json.loads(metadata_raw), json.loads(kb_raw), trace_id, allow_ai=repair
        )

        db.run(
            "UPDATE dashboards SET dashboard_intent = %s WHERE dashboard_id = %s",
            (json.dumps(spec), dashboard_id),
        )
        return _payload(dashboard_id, spec, data, trace_id, reused=True)

    except HTTPException:
        raise
    except Exception as e:
        log_ai_event(trace_id, "dashboard.data_failed", level="error", error=str(e), dashboard_id=dashboard_id)
        logger.exception("Dashboard data failed for %s (trace %s)", dashboard_id, trace_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DASHBOARD_DATA_FAILED",
                "message": "I couldn't load this dashboard's data.",
                "retryable": True,
                "trace_id": trace_id,
            },
        )

class ShareDashboardRequest(BaseModel):
    userId: int
    dashboard_id: int
    enabled: bool = True
    refresh: bool = False

@router.post("/share_dashboard")
def share_dashboard(payload: ShareDashboardRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    trace_id = new_trace_id()
    try:
        _check_dashboard_owner(payload.dashboard_id, payload.userId)

        if not payload.enabled:
            db.run(
                "UPDATE dashboards SET is_shared = 0, share_token = NULL, shared_snapshot = NULL, "
                "shared_at = NULL WHERE dashboard_id = %s",
                (payload.dashboard_id,),
            )
            log_ai_event(trace_id, "dashboard.share_disabled", dashboard_id=payload.dashboard_id)
            return {"dashboard_id": payload.dashboard_id, **_share_state(payload.dashboard_id)}

        row = db.fetch(
            "SELECT dashboard_intent, table_id, is_shared, share_token FROM dashboards "
            "WHERE dashboard_id = %s AND is_deleted = 0",
            params=(payload.dashboard_id,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Dashboard not found.")
        intent_raw, table_id, is_shared, token = row[0]

        if is_shared and token and not payload.refresh:
            return {"dashboard_id": payload.dashboard_id, **_share_state(payload.dashboard_id)}

        table_name, metadata_raw, kb_raw = _load_table_context(table_id)
        spec, data = dashboard_service.build_dashboard(
            json.loads(intent_raw), table_name, json.loads(metadata_raw), json.loads(kb_raw),
            trace_id, allow_ai=False,
        )
        snapshot = dashboard_service.public_snapshot(spec, data)
        if not snapshot["data"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "NOTHING_TO_SHARE",
                    "message": "None of this dashboard's widgets could load, so there is nothing to share yet.",
                    "retryable": False,
                    "trace_id": trace_id,
                },
            )

        token = token or secrets.token_urlsafe(24)
        db.run(
            "UPDATE dashboards SET is_shared = 1, share_token = %s, shared_snapshot = %s, "
            "shared_at = SYSDATETIME(), dashboard_intent = %s WHERE dashboard_id = %s",
            (token, json.dumps(snapshot, default=str), json.dumps(spec), payload.dashboard_id),
        )
        log_ai_event(trace_id, "dashboard.share_enabled", dashboard_id=payload.dashboard_id,
                     refreshed=payload.refresh)
        return {"dashboard_id": payload.dashboard_id, **_share_state(payload.dashboard_id)}

    except HTTPException:
        raise
    except Exception as e:
        log_ai_event(trace_id, "dashboard.share_failed", level="error", error=str(e),
                     dashboard_id=payload.dashboard_id)
        logger.exception("Share failed for dashboard %s (trace %s)", payload.dashboard_id, trace_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "SHARE_FAILED",
                "message": "I couldn't update sharing for this dashboard. Please try again.",
                "retryable": True,
                "trace_id": trace_id,
            },
        )

class SetDashboardStatusRequest(BaseModel):
    userId: int
    dashboard_id: int
    status: str

@router.post("/set_dashboard_status")
def set_dashboard_status(payload: SetDashboardStatusRequest, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)
    if payload.status not in ("Live", "Inactive"):
        raise HTTPException(status_code=400, detail="status must be 'Live' or 'Inactive'.")

    _check_dashboard_owner(payload.dashboard_id, payload.userId)

    db.run(
        "UPDATE dashboards SET status = %s WHERE dashboard_id = %s",
        (payload.status, payload.dashboard_id),
    )
    return {"dashboard_id": payload.dashboard_id, "status": payload.status}

@router.post("/delete_dashboard")
def delete_dashboard(dashboard_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    _check_dashboard_owner(dashboard_id, userId)

    db.run(
        "UPDATE dashboards SET is_deleted = 1, deleted_at = SYSDATETIME(), is_shared = 0, "
        "share_token = NULL, shared_snapshot = NULL, shared_at = NULL WHERE dashboard_id = %s",
        (dashboard_id,),
    )
    return {"dashboard_id": dashboard_id, "deleted": True}