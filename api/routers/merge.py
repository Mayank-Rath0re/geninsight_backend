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

from core.ai_trace import log_ai_event, new_trace_id
from core.security import get_current_user_id, require_same_user
from services import merge, usage_service
from services.custom_db import execution
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
    # Uploaded files live in our database, so they can't be joined into a
    # session that reads from a client-owned database. Fail before ingesting.
    if execution.session_data_source_id(targetSessionId) is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This session is backed by a custom database, so a file cannot be merged "
                "into it. Connect the new data as its own database instead."
            ),
        )
    usage_service.assert_can_upload(userId)
    usage_service.check_quota(userId)  # KB build + merge planning are AI calls
    trace_id = new_trace_id()
    with usage_service.metered_call(userId, "merge_upload", trace_id) as meter:
        try:
            ingest_result = await ingest_table_and_session(
                user_id=userId,
                file=file,
                apply_basic_transformation=apply_basic_transformation,
            )
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "merge_upload: ingest failed for user %s, file %s (trace %s)",
                userId, file.filename, trace_id,
            )
            log_ai_event(trace_id, "merge.ingest_failed", level="error", filename=file.filename)
            raise HTTPException(
                status_code=500,
                detail={"code": "INGEST_FAILED",
                        "message": "That file couldn't be processed. Check the format and try again.",
                        "retryable": True, "trace_id": trace_id},
            )
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
            meter.count_query = meter.llm_calls > 0
            return result
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "merge_upload: merge failed (target=%s, new source=%s, trace %s)",
                targetSessionId, ingest_result["session_id"], trace_id,
            )
            log_ai_event(trace_id, "merge.failed", level="error", mode="upload")
            raise HTTPException(
                status_code=500,
                detail={"code": "MERGE_FAILED",
                        "message": "The merge couldn't be completed. Please try again.",
                        "retryable": True, "trace_id": trace_id},
            )


class MergeSessionRequest(BaseModel):
    userId: int
    targetSessionId: int
    sourceSessionId: int
    hint: str = ""


@router.post("/merge_session")
def merge_session(payload: MergeSessionRequest, authed_user_id: int = Depends(get_current_user_id)):
    """Merges an already-existing session into targetSessionId as a join step (1 AI call)."""
    require_same_user(authed_user_id, payload.userId)
    usage_service.check_quota(payload.userId)
    trace_id = new_trace_id()
    with usage_service.metered_call(payload.userId, "merge", trace_id) as meter:
        try:
            result = merge.perform_merge(
                target_session_id=payload.targetSessionId,
                source_session_id=payload.sourceSessionId,
                user_id=payload.userId,
                user_hint=payload.hint,
            )
            meter.count_query = meter.llm_calls > 0
            return result
        except HTTPException:
            raise
        except Exception:
            logger.exception(
                "merge_session failed (target=%s, source=%s, trace %s)",
                payload.targetSessionId, payload.sourceSessionId, trace_id,
            )
            log_ai_event(trace_id, "merge.failed", level="error", mode="session")
            raise HTTPException(
                status_code=500,
                detail={"code": "MERGE_FAILED",
                        "message": "The merge couldn't be completed. Please try again.",
                        "retryable": True, "trace_id": trace_id},
            )