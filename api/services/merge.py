# services/merge.py
"""
Orchestrates a "merge session B into session A" operation. This is the
shared core called by both merge entry points (upload-a-new-file-then-
merge, and merge-an-existing-session) — the only difference between those
two flows is how source_session_id comes to exist; everything after that
is identical.

A merge:
  1. Resolves the CURRENT pipeline output schema of both sessions
     (target's {prev} and source's {merge}), plus each session's root
     table's knowledge base for LLM context.
  2. Calls services.merge_generation.get_merge_sql to plan + generate a
     single join SELECT referencing the literal placeholders {prev} and
     {merge}.
  3. Persists it as a new `queries` row with step_type='join', linked
     into the TARGET session via session_queries (so it takes its place
     as the next step in the target session's chain — exactly like an
     ordinary transform step) and into query_merges (recording exactly
     which session/query/table was merged in, frozen at this moment).
  4. Links query_tables for every base table on both sides, so
     /get_session's existing table-listing logic keeps working unchanged
     for join steps.
  5. Runs the pipeline (services.pipeline.run_pipeline_result), which
     will splice in the source session's own chain via {merge}.
  6. On pipeline failure, rolls back exactly like /transform does —
     deletes the rows it just committed so a broken merge never lingers
     in history looking valid.

The source session is READ-ONLY throughout this entire flow. Nothing here
ever writes to the source session's sessions/queries/session_queries rows
— it stays fully independent and usable on its own afterward, per product
requirement.
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
    Returns {"id", "name", "knowledgebase", "metadata"} for the FIRST
    table ever linked into this session (its root/original table) — used
    as the semantic anchor for LLM context even if the session has since
    accumulated many transform steps. A session's root table is the
    table linked to its earliest query_tables row.
    """
    rows = db.fetch(
        """
        SELECT ti.id, ti.name, ti.knowledgebase, ti.metadata
        FROM session_queries sq
        JOIN query_tables qt   ON qt.query_id = sq.query_id
        JOIN table_info   ti   ON ti.id       = qt.table_id
        WHERE sq.session_id = %s
        ORDER BY sq.date_created ASC
        """,
        params=(session_id,),
    )
    if not rows:
        raise ValueError(f"Session {session_id} has no tables linked to it yet.")
    r = rows[0]
    return {"id": r[0], "name": r[1], "knowledgebase": r[2], "metadata": r[3]}


def _get_latest_query_id(session_id: int):
    """Returns the most recent query_id in session_id's chain, or None if the session has no steps."""
    rows = db.fetch(
        "SELECT TOP 1 query_id FROM session_queries WHERE session_id = %s ORDER BY date_created DESC",
        params=(session_id,),
    )
    return rows[0][0] if rows else None


def _get_query_columns(query_id: int):
    row = db.fetch("SELECT updated_columns FROM queries WHERE id = %s", params=(query_id,))
    if not row:
        return None
    return json.loads(row[0][0]) if row[0][0] else []


def _all_base_table_ids_for_session(session_id: int) -> list:
    rows = db.fetch(
        """
        SELECT DISTINCT qt.table_id
        FROM session_queries sq
        JOIN query_tables qt ON qt.query_id = sq.query_id
        WHERE sq.session_id = %s
        """,
        params=(session_id,),
    )
    return [r[0] for r in rows]


def perform_merge(target_session_id: int, source_session_id: int, user_id: int, user_hint: str = "") -> dict:
    """
    Merges source_session_id's current pipeline output into
    target_session_id as a new join step. Returns the same response
    shape as /transform: {"sessionId", "query": {...}, "data": {...}}.

    Raises HTTPException for all user-facing failure cases (ownership
    mismatch, empty/missing sessions, infeasible join, pipeline failure)
    so both router entry points can call this directly and let the
    exception propagate.
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
    target_latest_qid = _get_latest_query_id(target_session_id)
    if target_latest_qid is None:
        raise HTTPException(
            status_code=422,
            detail="Target session has no steps yet — upload/select its table before merging into it.",
        )
    target_cols = _get_query_columns(target_latest_qid)
    try:
        target_root_table = _get_session_root_table(target_session_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    target_kb = json.loads(target_root_table["knowledgebase"]) if target_root_table["knowledgebase"] else {}

    # ── Resolve source side ({merge}) ──
    try:
        source_root_table = _get_session_root_table(source_session_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    source_kb = json.loads(source_root_table["knowledgebase"]) if source_root_table["knowledgebase"] else {}

    source_latest_qid = _get_latest_query_id(source_session_id)
    if source_latest_qid is not None:
        source_cols = _get_query_columns(source_latest_qid)
    else:
        # Source session has no transform steps yet — merge against its
        # raw table directly. source_query_id stays NULL in query_merges
        # (pipeline.py treats that as "-1", i.e. the full/only chain,
        # which for a step-less session resolves to... nothing to chain,
        # so we require at least a virtual first step. In practice every
        # session created via /upload or /transform already has >=1 step
        # by the time it's mergeable — see routers layer, which always
        # creates an initial step for a freshly-uploaded root table.)
        raise HTTPException(
            status_code=422,
            detail="Source session has no steps yet — it must have at least its initial "
                   "load step before it can be merged from.",
        )

    if not target_cols or not source_cols:
        raise HTTPException(
            status_code=422,
            detail="Could not resolve current column schema for one or both sessions.",
        )

    # ── Stage 1+2: LLM plan + generate the join SQL ──
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

    target_table_ids = _all_base_table_ids_for_session(target_session_id)
    all_table_ids = list(set(target_table_ids) | {source_root_table["id"]})

    # ── Persist: queries row (step_type='join') + query_tables + session_queries + query_merges ──
    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO queries (prompt, sql_query, summary, updated_columns, step_type) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s)",
                (
                    user_hint or f"Merge with session {source_session_id} ({source_root_table['name']})",
                    sql_query,
                    merge_result["summary"],
                    updated_columns,
                    "join",
                ),
            )
            row = cur.fetchone()
            if not row:
                raise Exception("Failed to persist merge query object.")
            query_obj_id = row[0]

            for table_id in all_table_ids:
                cur.execute(
                    "INSERT INTO query_tables (query_id, table_id) VALUES (%s, %s)",
                    (query_obj_id, table_id),
                )

            cur.execute(
                "INSERT INTO session_queries (session_id, query_id) VALUES (%s, %s)",
                (target_session_id, query_obj_id),
            )

            cur.execute(
                "INSERT INTO query_merges "
                "(query_id, source_session_id, source_query_id, source_table_id, join_type, join_summary) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    query_obj_id,
                    source_session_id,
                    source_latest_qid,
                    source_root_table["id"],
                    join_type,
                    join_summary,
                ),
            )

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
                db.run("DELETE FROM query_merges   WHERE query_id = %s", (query_obj_id,))
                db.run("DELETE FROM session_queries WHERE query_id = %s", (query_obj_id,))
                db.run("DELETE FROM query_tables    WHERE query_id = %s", (query_obj_id,))
                db.run("DELETE FROM queries         WHERE id = %s", (query_obj_id,))
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