"""Session lifecycle: history retrieval, listing, pipeline replay, rollback."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from core import db
from core.security import get_current_user_id, require_same_user
from services import pipeline

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["sessions"])


@router.get("/get_session")
def session_info(sessionId: int, userId: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    try:
        query1 = "SELECT TOP 1 session_id, user_id, table_id FROM sessions WHERE session_id = %s"
        output = db.fetch(query1, params=(sessionId,))
        if not output:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to fetch session for session id: {sessionId}"
            )
        if output[0][1] != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        session_table_id = output[0][2]
        native_table = None
        if session_table_id is not None:
            native_table_row = db.fetch(
                "SELECT id, name FROM table_info WHERE id = %s", params=(session_table_id,)
            )
            if native_table_row:
                native_table = {"id": native_table_row[0][0], "name": native_table_row[0][1]}

        query2 = (
            "SELECT id, prompt, sql_query, summary, updated_columns, step_type, "
            "       source_session_id, join_type, join_summary, created_at "
            "FROM queries WHERE session_id = %s AND is_deleted = 0 ORDER BY created_at"
        )
        output2 = db.fetch(query2, params=(sessionId,))
        if not output2:
            return {"session_id": sessionId, "query_history": [], "table": native_table}

        source_session_ids = list({row[6] for row in output2 if row[6] is not None})
        source_table_map: dict = {}
        if source_session_ids:
            placeholders = ", ".join(["%s"] * len(source_session_ids))
            query3 = (
                f"SELECT s.session_id, ti.id, ti.name "
                f"FROM sessions s JOIN table_info ti ON ti.id = s.table_id "
                f"WHERE s.session_id IN ({placeholders})"
            )
            output3 = db.fetch(query3, params=tuple(source_session_ids))
            source_table_map = {row[0]: {"id": row[1], "name": row[2]} for row in output3}

        query_history = []
        for row in output2:
            entry = {
                "id": row[0],
                "prompt": row[1],
                "sql_query": row[2],
                "summary": row[3],
                "updated_columns": json.loads(row[4]) if row[4] else [],
                "step_type": row[5],
                "table": native_table,
                "date_created": row[9].isoformat() if row[9] else None,
            }

            source_session_id = row[6]
            if source_session_id is not None:
                entry["merge"] = {
                    "source_session_id": source_session_id,
                    "source_table": source_table_map.get(source_session_id),
                    "join_type": row[7],
                    "join_summary": row[8],
                }

            query_history.append(entry)

        return {
            "session_id": sessionId,
            "query_history": query_history,
            "table": native_table,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch session %s", sessionId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch session: {str(e)}")


@router.get("/user_sessions")
def get_user_sessions(userId: int, search: str = "", all_mode: bool = False, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, userId)
    try:
        has_search = search != ""
        top_clause = "" if all_mode else "TOP 20 "
        query = f"SELECT {top_clause}session_id, session_title, date_modified FROM sessions WHERE user_id = %s AND is_deleted=0"
        if has_search:
            query += " AND session_title LIKE %s"
        params = (userId, f"%{search}%") if has_search else (userId,)
        rows = db.fetch(query, params=params)
        print(f"Total sessions for userId {userId}: {len(rows)}")
        if not rows:
            return {"history": []}
        return {"history": rows}
    except Exception:
        raise HTTPException(status_code=400, detail="Error fetching history!")


@router.post("/update_modified")
def update_session_metadata(session_id: int, user_id: int, session_title: str, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, user_id)
    query = "SELECT TOP 1 session_id, user_id FROM sessions WHERE session_id = %s AND user_id = %s"
    response = db.fetch(query=query, params=(session_id, user_id,))
    if response == []:
        raise HTTPException(status_code=400, detail="Session not found!")
    query2 = "UPDATE sessions SET session_title = %s, date_modified = SYSDATETIME() WHERE session_id = %s AND user_id = %s"
    db.run(query=query2, params=(session_title, session_id, user_id))


@router.post("/delete_session")
def delete_session(session_id: int, user_id: int, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, user_id)
    query = "SELECT TOP 1 session_id, user_id, table_id FROM sessions WHERE session_id = %s AND user_id = %s"
    response = db.fetch(query=query, params=(session_id, user_id,))
    if response == []:
        raise HTTPException(status_code=400, detail="Session not found!")
    table_id = response[0][2]
    query2 = "UPDATE sessions SET is_deleted=1, deleted_at = SYSDATETIME() WHERE session_id = %s AND user_id = %s"
    db.run(query=query2, params=(session_id, user_id))
    if table_id:
        db.run(
            query="UPDATE table_info SET is_deleted=1, deleted_at = SYSDATETIME() WHERE id = %s",
            params=(table_id,)
        )


@router.get("/pipeline_result")
def pipeline_result(sessionId: int, userId: int, queryId: int, authed_user_id: int = Depends(get_current_user_id)):
    """
    Re-runs the CTE-chained pipeline for sessionId, truncated to (and
    including) queryId. queryId=-1 runs the full pipeline. Used to
    preview any historical step without mutating it.
    """
    require_same_user(authed_user_id, userId)
    try:
        row = db.fetch(
            "SELECT TOP 1 user_id FROM sessions WHERE session_id = %s",
            params=(sessionId,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if row[0][0] != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        try:
            output = pipeline.run_pipeline_result(sessionId, queryId)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

        return {"sessionId": sessionId, "queryId": queryId, "data": output}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Pipeline result failed for session %s, query %s", sessionId, queryId
        )
        raise HTTPException(status_code=500, detail=f"Pipeline result failed: {str(e)}")


class RollbackRequest(BaseModel):
    sessionId: int
    userId: int
    queryId: int


@router.post("/rollback_transform")
def rollback_transform(payload: RollbackRequest, authed_user_id: int = Depends(get_current_user_id)):
    """
    Soft-deletes queryId and everything after it in the chain
    (is_deleted=1), consistent with queries.is_deleted/deleted_at.
    A join step's merge info lives inline on the same row, so deleting
    the row removes it entirely.

    Frontend note: on a failed call here, the Flutter client refetches
    /get_session to resync local history rather than trusting an
    optimistic local truncation.
    """
    sessionId = payload.sessionId
    userId = payload.userId
    queryId = payload.queryId
    require_same_user(authed_user_id, userId)
    try:
        with db.transaction() as cur:
            cur.execute("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", (sessionId,))
            row = cur.fetchone()
            if row is None or row[0] != userId:
                raise HTTPException(status_code=401, detail="User Mismatch")

            cur.execute(
                "SELECT id FROM queries WHERE session_id = %s AND is_deleted = 0 ORDER BY created_at ASC",
                (sessionId,),
            )
            ordered_ids = [r[0] for r in cur.fetchall()]
            if queryId not in ordered_ids:
                raise HTTPException(status_code=404, detail="Query not found in session")

            cut = ordered_ids.index(queryId)
            to_remove = ordered_ids[cut:]

            placeholders = ", ".join(["%s"] * len(to_remove))
            cur.execute(
                f"UPDATE queries SET is_deleted = 1, deleted_at = SYSDATETIME() WHERE id IN ({placeholders})",
                tuple(to_remove),
            )

        return {"sessionId": sessionId, "removed_query_ids": to_remove}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Rollback failed for session %s, query %s", sessionId, queryId)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")