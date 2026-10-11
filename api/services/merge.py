# services/merge.py
"""
Orchestrates a "merge session B into session A" operation. Shared core
called by both merge entry points (upload-a-new-file-then-merge, and
merge-an-existing-session) — the only difference between those two flows
is how source_session_id comes to exist; everything after that is
identical.

A merge:
  1. Resolves the CURRENT pipeline output schema of both sessions
     (target's {prev} and source's {merge}), plus each session's root
     table (sessions.table_id -> table_info) for LLM context.
  2. Calls services.merge_generation.get_merge_sql to plan + generate a
     single join SELECT referencing the literal placeholders {prev} and
     {merge}.
  3. Persists it as a new `queries` row with step_type='join',
     session_id=target_session_id, and source_session_id/join_type/
     join_summary set inline (per init.sql's chk_queries_join_fields —
     there is no separate query_merges table).
  4. Runs the pipeline (services.pipeline.run_pipeline_result), which
     splices in the source session's own chain via {merge}.
  5. On pipeline failure, rolls back exactly like /transform does —
     deletes the row it just committed so a broken merge never lingers
     in history looking valid.

The source session is READ-ONLY throughout. Nothing here ever writes to
the source session's own rows — it stays fully independent and usable on
its own afterward, per product requirement.
"""

import json
import logging

from fastapi import HTTPException

from core import db
from services import merge_generation, pipeline

logger = logging.getLogger("my_global_app_logger")


def _get_session_owner(session_id: int):
    row = db.fetch("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", params=(session_id,))
    if not row:
        return None
    return row[0][0]


def _get_session_root_table(session_id: int) -> dict:
    """
    Returns {"id", "name", "knowledgebase", "metadata"} for the table a
    session was created against (sessions.table_id -> table_info).
    """
    rows = db.fetch(
        """
        SELECT ti.id, ti.name, ti.knowledgebase, ti.metadata
        FROM sessions s
        JOIN table_info ti ON ti.id = s.table_id
        WHERE s.session_id = %s
        """,
        params=(session_id,),
    )
    if not rows:
        raise ValueError(f"Session {session_id} has no table linked to it.")
    r = rows[0]
    return {"id": r[0], "name": r[1], "knowledgebase": r[2], "metadata": r[3]}


def _get_latest_query_id(session_id: int):
    """Returns the most recent (non-deleted) query id in session_id's chain, or None."""
    rows = db.fetch(
        "SELECT TOP 1 id FROM queries WHERE session_id = %s AND is_deleted = 0 ORDER BY created_at DESC",
        params=(session_id,),
    )
    return rows[0][0] if rows else None


def _get_query_columns(query_id: int):
    row = db.fetch("SELECT updated_columns FROM queries WHERE id = %s", params=(query_id,))
    if not row:
        return None
    return json.loads(row[0][0]) if row[0][0] else []


def perform_merge(target_session_id: int, source_session_id: int, user_id: int, user_hint: str = "") -> dict:
    """
    Merges source_session_id's current pipeline output into
    target_session_id as a new join step. Returns the same response
    shape as /transform: {"sessionId", "query": {...}, "data": {...}}.
    """

    if target_session_id == source_session_id:
        raise HTTPException(status_code=400, detail="Cannot merge a session into itself.")

    target_owner = _get_session_owner(target_session_id)
    if target_owner is None:
        raise HTTPException(status_code=404, detail=f"Target session {target_session_id} not found")
    if target_owner != user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")

    source_owner = _get_session_owner(source_session_id)
    if source_owner is None:
        raise HTTPException(status_code=404, detail=f"Source session {source_session_id} not found")
    if source_owner != user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")

    # ── Resolve target side ({prev}) ──
    target_root_table = _get_session_root_table(target_session_id)
    target_kb = json.loads(target_root_table["knowledgebase"]) if target_root_table["knowledgebase"] else {}

    target_latest_qid = _get_latest_query_id(target_session_id)
    target_cols = _get_query_columns(target_latest_qid) if target_latest_qid is not None else None

    # ── Resolve source side ({merge}) ──
    source_root_table = _get_session_root_table(source_session_id)
    source_kb = json.loads(source_root_table["knowledgebase"]) if source_root_table["knowledgebase"] else {}

    source_latest_qid = _get_latest_query_id(source_session_id)
    if source_latest_qid is None:
        raise HTTPException(
            status_code=422,
            detail="Source session has no steps yet — it must have at least its initial "
                   "load step before it can be merged from.",
        )
    source_cols = _get_query_columns(source_latest_qid)

    if not target_cols or not source_cols:
        raise HTTPException(
            status_code=422,
            detail="Could not resolve current column schema for one or both sessions.",
        )

    # ── LLM plan + generate the join SQL ──
    try:
        merge_result = merge_generation.get_merge_sql(
            target_schema_cols=target_cols,
            target_context=target_kb,
            source_schema_cols=source_cols,
            source_context=source_kb,
            user_hint=user_hint,
        )
    except Exception as e:
        logger.exception("Merge SQL generation failed (target=%s, source=%s)", target_session_id, source_session_id)
        raise HTTPException(status_code=500, detail=f"Merge generation failed: {str(e)}")

    sql_query = merge_result["sql"]
    if "Requested fields not found in schema" in sql_query:
        raise HTTPException(status_code=422, detail=merge_result["summary"])

    updated_columns = json.dumps(merge_result["columns"])
    join_type = merge_result["join_type"] or "INNER"
    join_summary = merge_result["join_summary"] or merge_result["summary"]

    # ── Persist: single queries row carries everything (step_type='join') ──
    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO queries "
                "(session_id, prompt, sql_query, summary, updated_columns, step_type, "
                " source_session_id, join_type, join_summary) "
                "OUTPUT INSERTED.id "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    target_session_id,
                    user_hint or f"Merge with session {source_session_id} ({source_root_table['name']})",
                    sql_query,
                    merge_result["summary"],
                    updated_columns,
                    "join",
                    source_session_id,
                    join_type,
                    join_summary,
                ),
            )
            row = cur.fetchone()
            if not row:
                raise Exception("Failed to persist merge query object.")
            query_obj_id = row[0]

            cur.execute(
                "SELECT id, prompt, sql_query, summary, updated_columns FROM queries WHERE id = %s",
                (query_obj_id,),
            )
            q = cur.fetchone()

        # commits here — pipeline runs after, same pattern as /transform

        try:
            output = pipeline.run_pipeline_result(target_session_id, query_obj_id)
        except Exception:
            try:
                db.run("DELETE FROM queries WHERE id = %s", (query_obj_id,))
            except Exception:
                logger.exception("Cleanup failed for merge query %s after pipeline error", query_obj_id)
            raise

        return {
            "sessionId": target_session_id,
            "query": {
                "id": q[0],
                "prompt": q[1],
                "sql_query": q[2],
                "summary": q[3],
                "updated_columns": json.loads(q[4]) if q[4] else [],
                "step_type": "join",
                "merge": {
                    "source_session_id": source_session_id,
                    "source_query_id": source_latest_qid,
                    "source_table_id": source_root_table["id"],
                    "source_table_name": source_root_table["name"],
                    "join_type": join_type,
                    "join_summary": join_summary,
                },
            },
            "data": output,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Merge failed (target=%s, source=%s)", target_session_id, source_session_id)
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)}")
