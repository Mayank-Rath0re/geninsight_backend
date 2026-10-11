# routers/query.py
"""
Single-query-step read/delete. Was imported by main.py but missing from
the codebase entirely (app could not start). Session-scoped multi-step
history already lives in GET /get_session; this router covers fetching
or removing one step in isolation, which the frontend needs for things
like a "view this step's SQL" panel independent of full history reload.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from core import db
from core.security import get_current_user_id, require_same_user

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["query"])


@router.get("/query")
def get_query(queryId: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    row = db.fetch(
        """
        SELECT q.id, q.session_id, q.prompt, q.sql_query, q.summary, q.updated_columns,
               q.step_type, q.source_session_id, q.join_type, q.join_summary,
               q.created_at, s.user_id
        FROM queries q
        JOIN sessions s ON s.session_id = q.session_id
        WHERE q.id = %s AND q.is_deleted = 0
        """,
        params=(queryId,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")
    r = row[0]
    if r[11] != userId:
        raise HTTPException(status_code=401, detail="User Mismatch")

    return {
        "id": r[0],
        "session_id": r[1],
        "prompt": r[2],
        "sql_query": r[3],
        "summary": r[4],
        "updated_columns": json.loads(r[5]) if r[5] else [],
        "step_type": r[6],
        "source_session_id": r[7],
        "join_type": r[8],
        "join_summary": r[9],
        "created_at": r[10].isoformat() if r[10] else None,
    }


@router.post("/delete_query")
def delete_query(queryId: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    """Soft-deletes a single step. For deleting a step AND everything after
    it in the chain, use /rollback_transform instead."""
    require_same_user(authed_user_id, userId)
    row = db.fetch(
        """
        SELECT q.id FROM queries q
        JOIN sessions s ON s.session_id = q.session_id
        WHERE q.id = %s AND s.user_id = %s AND q.is_deleted = 0
        """,
        params=(queryId, userId),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Query not found")

    db.run(
        "UPDATE queries SET is_deleted = 1, deleted_at = SYSDATETIME() WHERE id = %s",
        (queryId,),
    )
    return {"queryId": queryId, "deleted": True}
