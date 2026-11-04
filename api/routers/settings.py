import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db
from core.rate_limit import RateLimiter
from core.security import get_current_user_id, hash_password, require_same_user, verify_password
from services import dataset_cleanup

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["settings"])

sensitive_limiter = RateLimiter(max_calls=5, window_seconds=60)

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_NAME_LENGTH = 100

def _iso(value):
    return value.isoformat() if value else None

def _profile(user_id: int) -> dict:
    rows = db.fetch(
        "SELECT id, email, name, created_at FROM auth WHERE id = %s AND is_deleted = 0",
        params=(user_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Account not found.")
    r = rows[0]
    return {"user_id": r[0], "email": r[1], "name": r[2], "created_at": _iso(r[3])}

@router.get("/me")
def get_me(authed_user_id: int = Depends(get_current_user_id)):
    return _profile(authed_user_id)

class UpdateProfileRequest(BaseModel):
    name: str

@router.put("/me")
def update_me(payload: UpdateProfileRequest, authed_user_id: int = Depends(get_current_user_id)):
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(status_code=400, detail="Name can't be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise HTTPException(status_code=400, detail=f"Name must be at most {MAX_NAME_LENGTH} characters.")
    db.run("UPDATE auth SET name = %s WHERE id = %s AND is_deleted = 0", (name, authed_user_id))
    return _profile(authed_user_id)

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    sign_out_everywhere: bool = False

@router.post("/change_password", dependencies=[Depends(sensitive_limiter)])
def change_password(payload: ChangePasswordRequest, authed_user_id: int = Depends(get_current_user_id)):
    if not (MIN_PASSWORD_LENGTH <= len(payload.new_password) <= MAX_PASSWORD_LENGTH):
        raise HTTPException(
            status_code=400,
            detail=f"New password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.",
        )

    stored = db.get_single_value(
        "SELECT password_hash FROM auth WHERE id = %s AND is_deleted = 0", (authed_user_id,)
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    if not verify_password(payload.current_password, stored):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if verify_password(payload.new_password, stored):
        raise HTTPException(status_code=400, detail="New password must be different from the current one.")

    new_hash = hash_password(payload.new_password)
    if payload.sign_out_everywhere:
        db.run(
            "UPDATE auth SET password_hash = %s, refresh_token = NULL, refresh_token_expires_at = NULL "
            "WHERE id = %s",
            (new_hash, authed_user_id),
        )
    else:
        db.run("UPDATE auth SET password_hash = %s WHERE id = %s", (new_hash, authed_user_id))

    logger.info("Password changed for user %s (sign_out_everywhere=%s)", authed_user_id, payload.sign_out_everywhere)
    return {"message": "Password updated.", "signed_out": payload.sign_out_everywhere}

@router.post("/sign_out_everywhere", dependencies=[Depends(sensitive_limiter)])
def sign_out_everywhere(authed_user_id: int = Depends(get_current_user_id)):
    db.run(
        "UPDATE auth SET refresh_token = NULL, refresh_token_expires_at = NULL WHERE id = %s",
        (authed_user_id,),
    )
    return {"signed_out": True}

def _current_plan(user_id: int) -> dict:
    rows = db.fetch(
        "SELECT TOP 1 p.name, p.price, p.currency, p.billing_interval, p.max_tables, "
        "p.max_queries_per_month, p.max_data_sources, s.status, s.current_period_end, s.cancel_at_period_end "
        "FROM subscriptions s JOIN plans p ON p.id = s.plan_id "
        "WHERE s.user_id = %s AND s.is_deleted = 0 AND s.status IN ('active', 'trialing', 'past_due') "
        "ORDER BY s.created_at DESC, s.id DESC",
        params=(user_id,),
    )
    if rows:
        r = rows[0]
        return {
            "name": r[0], "price": float(r[1]), "currency": r[2], "billing_interval": r[3],
            "max_tables": r[4], "max_queries_per_month": r[5], "max_data_sources": r[6],
            "status": r[7], "renews_at": _iso(r[8]), "cancel_at_period_end": bool(r[9]),
        }
    rows = db.fetch(
        "SELECT TOP 1 name, price, currency, billing_interval, max_tables, max_queries_per_month, "
        "max_data_sources FROM plans WHERE name = 'Free' AND is_active = 1 ORDER BY id"
    )
    if rows:
        r = rows[0]
        return {
            "name": r[0], "price": float(r[1]), "currency": r[2], "billing_interval": r[3],
            "max_tables": r[4], "max_queries_per_month": r[5], "max_data_sources": r[6],
            "status": "active", "renews_at": None, "cancel_at_period_end": False,
        }
    return {
        "name": "Free", "price": 0.0, "currency": "INR", "billing_interval": "monthly",
        "max_tables": None, "max_queries_per_month": None, "max_data_sources": None,
        "status": "active", "renews_at": None, "cancel_at_period_end": False,
    }

def _count(query: str, user_id: int) -> int:
    return int(db.get_single_value(query, (user_id,)) or 0)

@router.get("/usage")
def get_usage(authed_user_id: int = Depends(get_current_user_id)):
    uid = authed_user_id
    return {
        "plan": _current_plan(uid),
        "usage": {
            "tables": _count("SELECT COUNT(*) FROM table_info WHERE user_id = %s AND is_deleted = 0", uid),
            "queries_this_month": _count(
                "SELECT COUNT(*) FROM queries q JOIN sessions s ON s.session_id = q.session_id "
                "WHERE s.user_id = %s AND q.is_deleted = 0 AND q.prompt <> 'Load data' "
                "AND q.created_at >= DATEFROMPARTS(YEAR(SYSDATETIME()), MONTH(SYSDATETIME()), 1)",
                uid,
            ),
            "data_sources": _count("SELECT COUNT(*) FROM data_sources WHERE user_id = %s AND is_deleted = 0", uid),
            "dashboards": _count("SELECT COUNT(*) FROM dashboards WHERE user_id = %s AND is_deleted = 0", uid),
            "shared_dashboards": _count(
                "SELECT COUNT(*) FROM dashboards WHERE user_id = %s AND is_deleted = 0 AND is_shared = 1", uid
            ),
        },
    }

@router.get("/settings_data")
def get_settings_data(authed_user_id: int = Depends(get_current_user_id)):
    datasets = db.fetch(
        "SELECT t.id, t.name, t.createdAt, "
        "(SELECT COUNT(*) FROM sessions s WHERE s.table_id = t.id AND s.is_deleted = 0), "
        "(SELECT COUNT(*) FROM dashboards d WHERE d.table_id = t.id AND d.is_deleted = 0) "
        "FROM table_info t WHERE t.user_id = %s AND t.is_deleted = 0 ORDER BY t.createdAt DESC, t.id DESC",
        params=(authed_user_id,),
    )
    dashboards = db.fetch(
        "SELECT d.dashboard_id, d.table_id, t.name, d.dashboard_intent, d.updated_at, d.is_shared, d.share_token "
        "FROM dashboards d JOIN table_info t ON t.id = d.table_id "
        "WHERE d.user_id = %s AND d.is_deleted = 0 ORDER BY d.updated_at DESC, d.dashboard_id DESC",
        params=(authed_user_id,),
    )

    dashboard_rows = []
    for r in dashboards:
        title = None
        try:
            title = (json.loads(r[3]) or {}).get("dashboard_title") if r[3] else None
        except ValueError:
            title = None
        shared = bool(r[5])
        dashboard_rows.append({
            "dashboard_id": r[0],
            "table_id": r[1],
            "table_name": r[2],
            "title": title or r[2],
            "updated_at": _iso(r[4]),
            "is_shared": shared,
            "share_token": r[6] if shared else None,
        })

    return {
        "datasets": [
            {
                "id": r[0],
                "name": r[1],
                "created_at": _iso(r[2]),
                "session_count": int(r[3] or 0),
                "dashboard_count": int(r[4] or 0),
            }
            for r in datasets
        ],
        "dashboards": dashboard_rows,
    }

@router.post("/delete_dataset")
def delete_dataset(table_id: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    row = db.fetch(
        "SELECT user_id, name FROM table_info WHERE id = %s AND is_deleted = 0",
        params=(table_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    owner, table_name = row[0]
    if owner != userId:
        raise HTTPException(status_code=401, detail="User Mismatch")

    with db.transaction() as cur:
        dataset_cleanup.soft_delete_dataset(cur, table_id)
    data_removed = dataset_cleanup.drop_physical_table_if_unused(table_id, table_name)
    logger.info("Dataset %s deleted by user %s (data_removed=%s)", table_id, userId, data_removed)
    return {"table_id": table_id, "deleted": True, "data_removed": data_removed}


class DeleteAccountRequest(BaseModel):
    password: str
    confirm_email: str

@router.post("/delete_account", dependencies=[Depends(sensitive_limiter)])
def delete_account(payload: DeleteAccountRequest, authed_user_id: int = Depends(get_current_user_id)):
    rows = db.fetch(
        "SELECT email, password_hash FROM auth WHERE id = %s AND is_deleted = 0",
        params=(authed_user_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Account not found.")
    email, stored = rows[0]

    if payload.confirm_email.strip().lower() != email.lower():
        raise HTTPException(status_code=400, detail="The email you typed doesn't match your account.")
    if not verify_password(payload.password, stored):
        raise HTTPException(status_code=400, detail="Password is incorrect.")

    paid = int(db.get_single_value(
        "SELECT COUNT(*) FROM subscriptions s JOIN plans p ON p.id = s.plan_id "
        "WHERE s.user_id = %s AND s.is_deleted = 0 AND s.status IN ('active', 'trialing', 'past_due') "
        "AND p.price > 0",
        (authed_user_id,),
    ) or 0)
    if paid:
        raise HTTPException(
            status_code=409,
            detail="Cancel your paid subscription before deleting your account.",
        )

    owned_tables = db.fetch(
        "SELECT id, name FROM table_info WHERE user_id = %s AND is_deleted = 0",
        params=(authed_user_id,),
    )

    with db.transaction() as cur:
        cur.execute(
            "UPDATE dashboards SET is_deleted = 1, deleted_at = SYSDATETIME(), is_shared = 0, "
            "share_token = NULL, shared_snapshot = NULL, shared_at = NULL "
            "WHERE user_id = %s AND is_deleted = 0",
            (authed_user_id,),
        )
        cur.execute(
            "UPDATE sessions SET is_deleted = 1, deleted_at = SYSDATETIME() "
            "WHERE user_id = %s AND is_deleted = 0",
            (authed_user_id,),
        )
        cur.execute(
            "UPDATE table_info SET is_deleted = 1, deleted_at = SYSDATETIME() "
            "WHERE user_id = %s AND is_deleted = 0",
            (authed_user_id,),
        )
        cur.execute(
            "UPDATE data_sources SET is_deleted = 1, deleted_at = SYSDATETIME(), status = 'revoked', "
            "secret_encrypted = '' WHERE user_id = %s AND is_deleted = 0",
            (authed_user_id,),
        )
        cur.execute(
            "UPDATE auth SET is_deleted = 1, deleted_at = SYSDATETIME(), refresh_token = NULL, "
            "refresh_token_expires_at = NULL, email = LEFT(CONCAT('deleted+', id, '+', email), 255) "
            "WHERE id = %s",
            (authed_user_id,),
        )
    for table_id, table_name in owned_tables:
        dataset_cleanup.drop_physical_table_if_unused(table_id, table_name)
    logger.info("Account %s deleted", authed_user_id)
    return {"deleted": True}