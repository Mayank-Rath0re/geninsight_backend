# routers/sessions.py
"""Session lifecycle: history retrieval, listing, pipeline replay, rollback."""

import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from core import db
from services import pipeline

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["sessions"])


@router.get("/get_session")
def session_info(sessionId: int, userId: int):
    try:
        query1 = "SELECT TOP 1 * FROM sessions WHERE session_id = %s"
        output = db.fetch(query1, params=(sessionId,))
        if not output:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to fetch session for session id: {sessionId}"
            )
        if output[0][1] != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        query2 = "SELECT * FROM session_queries WHERE session_id = %s"
        output2 = db.fetch(query2, params=(sessionId,))
        if not output2:
            return {"session_id": sessionId, "query_history": [], "tables": []}

        query_ids = [row[1] for row in output2]
        query_id_placeholders = ", ".join(["%s"] * len(query_ids))

        query3 = f"SELECT * FROM query_tables WHERE query_id IN ({query_id_placeholders})"
        output3 = db.fetch(query3, params=tuple(query_ids))
        if not output3:
            raise HTTPException(status_code=404, detail="Error fetching tables for history")

        table_ids = list(set(table[1] for table in output3))
        table_id_placeholders = ", ".join(["%s"] * len(table_ids))

        query4 = f"SELECT id, name FROM table_info WHERE id IN ({table_id_placeholders})"
        output4 = db.fetch(query4, params=tuple(table_ids))
        if not output4:
            raise HTTPException(status_code=404, detail="Error: Empty Table Info")

        # step_type is now selected explicitly (rather than SELECT *) so
        # positional indexing below stays stable regardless of future
        # column additions to `queries`.
        query5 = (
            f"SELECT id, prompt, sql_query, summary, updated_columns, step_type "
            f"FROM queries WHERE id IN ({query_id_placeholders})"
        )
        output5 = db.fetch(query5, params=tuple(query_ids))
        if not output5:
            raise HTTPException(status_code=404, detail="error fetching transform history")

        # Merge metadata for any join steps in this batch of query_ids —
        # fetched in one IN(...) query, not N+1, consistent with the rest
        # of this endpoint's batching style.
        query6 = (
            f"SELECT qm.query_id, qm.source_session_id, qm.source_query_id, "
            f"       qm.source_table_id, ti.name, qm.join_type, qm.join_summary "
            f"FROM query_merges qm "
            f"JOIN table_info ti ON ti.id = qm.source_table_id "
            f"WHERE qm.query_id IN ({query_id_placeholders})"
        )
        output6 = db.fetch(query6, params=tuple(query_ids))
        merge_map = {
            row[0]: {
                "source_session_id": row[1],
                "source_query_id": row[2],
                "source_table_id": row[3],
                "source_table_name": row[4],
                "join_type": row[5],
                "join_summary": row[6],
            }
            for row in output6
        }

        table_map = {row[0]: row[1] for row in output4}  # {table_id: table_name}

        query_table_map: dict = {}
        for row in output3:
            qid, tid = row[0], row[1]
            query_table_map.setdefault(qid, []).append(tid)

        sorted_session_queries = sorted(output2, key=lambda x: x[2])  # x[2] = date_created

        query_history = []
        for sq in sorted_session_queries:
            qid = sq[1]
            query_row = next((q for q in output5 if q[0] == qid), None)
            if not query_row:
                continue

            associated_table_ids = query_table_map.get(qid, [])
            associated_tables = [
                {"id": tid, "name": table_map.get(tid)}
                for tid in associated_table_ids
            ]

            entry = {
                "id": query_row[0],
                "prompt": query_row[1],
                "sql_query": query_row[2],
                "summary": query_row[3],
                "updated_columns": json.loads(query_row[4]) if query_row[4] else [],
                "step_type": query_row[5],
                "tables": associated_tables,
                "date_created": sq[2].isoformat() if sq[2] else None,
                "date_modified": sq[3].isoformat() if sq[3] else None,
            }

            if qid in merge_map:
                entry["merge"] = merge_map[qid]

            query_history.append(entry)

        session_tables = [{"id": tid, "name": name} for tid, name in table_map.items()]

        return {
            "session_id": sessionId,
            "query_history": query_history,
            "tables": session_tables,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch session %s", sessionId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch session: {str(e)}")


@router.get("/user_sessions")
def get_user_sessions(userId: int, search: str = ""):
    """
    Returns a lightweight summary of every session belonging to userId,
    most-recent first: session id, a title (first prompt in that session,
    since sessions have no name column), the step count, and last-touched
    timestamp. `search` (optional) filters by substring match against that
    title so the popup's search field has something to hit server-side.

    NOTE: this list is used both for the sidebar's session history AND as
    the candidate list for "merge from an existing session" — every
    session with >=1 step is a valid merge source (see services/merge.py).
    A session with step_count == 0 cannot be merged from yet; the
    frontend should treat step_count == 0 sessions as non-selectable in
    the merge picker rather than this endpoint filtering them out, since
    this endpoint is also used for plain session navigation where a
    step-less session may still be worth showing (e.g. "continue where
    you left off").
    """
    try:
        query = "SELECT session_id, type FROM sessions WHERE user_id = %s"
        rows = db.fetch(query, params=(userId,))
        if not rows:
            return []

        session_ids = [r[0] for r in rows]
        placeholders = ", ".join(["%s"] * len(session_ids))

        # Pull every session_queries row for these sessions in one go,
        # then group in Python — avoids N+1 queries.
        sq_query = (
            f"SELECT session_id, query_id, date_created "
            f"FROM session_queries WHERE session_id IN ({placeholders}) "
            f"ORDER BY date_created ASC"
        )
        sq_rows = db.fetch(sq_query, params=tuple(session_ids)) or []

        by_session: dict = {}
        for sid, qid, created in sq_rows:
            by_session.setdefault(sid, []).append((qid, created))

        all_query_ids = list({qid for _, qid, _ in sq_rows})
        prompt_map = {}
        if all_query_ids:
            qp = ", ".join(["%s"] * len(all_query_ids))
            q_rows = db.fetch(
                f"SELECT id, prompt FROM queries WHERE id IN ({qp})",
                params=tuple(all_query_ids),
            ) or []
            prompt_map = {qid: prompt for qid, prompt in q_rows}

        results = []
        for sid, session_type in rows:
            steps = by_session.get(sid, [])
            first_prompt = prompt_map.get(steps[0][0]) if steps else None
            last_touched = steps[-1][1] if steps else None
            title = first_prompt or f"Untitled session #{sid}"

            if search and search.lower() not in title.lower():
                continue

            results.append({
                "session_id": sid,
                "type": session_type,
                "title": title,
                "step_count": len(steps),
                "last_touched": last_touched.isoformat() if last_touched else None,
            })

        results.sort(key=lambda r: r["last_touched"] or "", reverse=True)
        return results

    except Exception as e:
        logger.exception("Failed to fetch sessions for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch sessions: {str(e)}")


@router.get("/pipeline_result")
def pipeline_result(sessionId: int, userId: int, queryId: int):
    """
    Re-runs the CTE-chained pipeline for `sessionId`, truncated to (and
    including) `queryId`. Pass queryId=-1 to run the full pipeline.
    Used by the frontend to preview any step in transformation history
    without mutating it (unlike /rollback_transform, this doesn't delete
    later steps).

    Transparently handles join steps in the chain — see
    services/pipeline.py's merge-aware assemble_cte_query — no special
    handling needed here.
    """
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
            # assemble_cte_query raises ValueError for bad query_id / broken chain
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
def rollback_transform(payload: RollbackRequest):
    """
    NOTE ON MERGE STEPS: rolling back a join step only removes that join
    step (and anything after it) from the TARGET session — it never
    touches the source session referenced in query_merges, consistent
    with merges being a one-directional, non-mutating snapshot of the
    source. query_merges rows for removed join steps cascade-delete
    automatically via fk_qm_query's ON DELETE CASCADE when the
    corresponding `queries` row is deleted below.
    """
    sessionId = payload.sessionId
    userId = payload.userId
    queryId = payload.queryId
    try:
        with db.transaction() as cur:
            cur.execute("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", (sessionId,))
            row = cur.fetchone()
            if row is None or row[0] != userId:
                raise HTTPException(status_code=401, detail="User Mismatch")

            cur.execute(
                "SELECT query_id FROM session_queries WHERE session_id = %s ORDER BY date_created ASC",
                (sessionId,),
            )
            ordered_ids = [r[0] for r in cur.fetchall()]
            if queryId not in ordered_ids:
                raise HTTPException(status_code=404, detail="Query not found in session")

            # Every step from queryId onward (inclusive) gets rolled back
            cut = ordered_ids.index(queryId)
            to_remove = ordered_ids[cut:]

            placeholders = ", ".join(["%s"] * len(to_remove))
            cur.execute(f"DELETE FROM session_queries WHERE query_id IN ({placeholders})", tuple(to_remove))
            cur.execute(f"DELETE FROM query_tables    WHERE query_id IN ({placeholders})", tuple(to_remove))
            cur.execute(f"DELETE FROM queries          WHERE id       IN ({placeholders})", tuple(to_remove))
            # query_merges rows for any join steps among to_remove are
            # cascade-deleted by fk_qm_query ON DELETE CASCADE — no
            # explicit DELETE needed here.

        return {"sessionId": sessionId, "removed_query_ids": to_remove}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Rollback failed for session %s, query %s", sessionId, queryId)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")