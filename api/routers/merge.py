"""
Two entry points for merging a second table's data into the current
session:

  POST /merge_upload  — user uploads a new file: ingests it exactly like
                         /upload (own new table + own new session, via
                         the shared services.table_ingest helper), then
                         merges that new session into the target session.

  POST /merge_session  — user picks an existing session as the source;
                          source_session_id is supplied directly.

Both delegate the join-planning/SQL-generation/persistence work to
services.merge.perform_merge. The source session is never mutated by
either flow.
"""

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from core.security import get_current_user_id, require_same_user
from services import merge
from services.table_ingest import ingest_table_and_session

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["merge"])


@router.post("/merge_upload")
async def merge_upload(
    userId: int,
    targetSessionId: int,
    apply_basic_transformation: bool = False,
    hint: str = Form(default=""),
    file: UploadFile = File(...),
    authed_user_id: int = Depends(get_current_user_id),
):
    """
    1. Ingests `file` via the shared table_ingest helper — same as
       /upload: new table_info row + new session + mechanical initial
       step (0 AI calls for this part).
    2. Merges that new session into targetSessionId as a join step
       (1 AI call — see services/merge_generation.py).
    """
    require_same_user(authed_user_id, userId)
    try:
        ingest_result = await ingest_table_and_session(
            user_id=userId,
            file=file,
            apply_basic_transformation=apply_basic_transformation,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("merge_upload: ingest failed for user %s, file %s", userId, file.filename)
        raise HTTPException(status_code=500, detail=f"Failed to ingest file for merge: {str(e)}")
    finally:
        await file.close()

    try:
        result = merge.perform_merge(
            target_session_id=targetSessionId,
            source_session_id=ingest_result["session_id"],
            user_id=userId,
            user_hint=hint,
        )
        result["newSourceSessionId"] = ingest_result["session_id"]
        result["newSourceTableId"] = ingest_result["table_id"]
        result["newSourceSessionTitle"] = ingest_result["session_title"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "merge_upload: merge failed (target=%s, new source=%s)",
            targetSessionId, ingest_result["session_id"],
        )
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)}")


class MergeSessionRequest(BaseModel):
    userId: int
    targetSessionId: int
    sourceSessionId: int
    hint: str = ""


@router.post("/merge_session")
def merge_session(payload: MergeSessionRequest, authed_user_id: int = Depends(get_current_user_id)):
    """Merges an already-existing session into targetSessionId as a join step (1 AI call)."""
    require_same_user(authed_user_id, payload.userId)
    return merge.perform_merge(
        target_session_id=payload.targetSessionId,
        source_session_id=payload.sourceSessionId,
        user_id=payload.userId,
        user_hint=payload.hint,
    )