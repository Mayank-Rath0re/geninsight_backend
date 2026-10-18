import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core import db
from core.rate_limit import RateLimiter

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["public"])

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
limiter = RateLimiter(max_calls=60, window_seconds=60)

@router.get("/shared_dashboard", dependencies=[Depends(limiter)])
def shared_dashboard(token: str):
    if not TOKEN_PATTERN.match(token):
        raise HTTPException(status_code=404, detail="This link is no longer available.")

    rows = db.fetch(
        "SELECT shared_snapshot, shared_at FROM dashboards "
        "WHERE share_token = %s AND is_shared = 1 AND is_deleted = 0",
        params=(token,),
    )
    if not rows or not rows[0][0]:
        raise HTTPException(status_code=404, detail="This link is no longer available.")

    try:
        snapshot = json.loads(rows[0][0])
    except ValueError:
        logger.exception("Corrupt shared snapshot")
        raise HTTPException(status_code=404, detail="This link is no longer available.")

    shared_at = rows[0][1]
    return JSONResponse(
        content={
            "dashboard_spec": snapshot.get("spec", {}),
            "data": snapshot.get("data", {}),
            "shared_at": shared_at.isoformat() if shared_at else None,
        },
        headers={"Cache-Control": "no-store"},
    )