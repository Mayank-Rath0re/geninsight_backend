"""
Creates a new transform step in an existing session's pipeline.

Session creation no longer happens here — every session is now created
synchronously at upload time (routers/datasets.py's /upload), seeded
with an initial mechanical "SELECT *" step. sessionId is therefore
always required and must already exist; this router only appends to it.

Billing: the public handler gates on the user's quota BEFORE any LLM spend and meters every
LLM call (plan, generate, repair) made while the step runs. A step counts toward the Free
monthly query cap only if it succeeds; tokens are recorded either way.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

import models
from core import db
from core.security import get_current_user_id, require_same_user
from services import pipeline, query_generation, usage_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["transform"])

from core.ai_trace import new_trace_id, log_ai_event
from core.sql_safety import classify_sql_error

MAX_SQL_ATTEMPTS = 2  # original + one AI repair
USER_MESSAGES = {
    "SQL_SYNTAX": "I couldn't build a valid query for that request. Try rephrasing it.",
    "SQL_ORDER_BY": "I couldn't apply that sorting. Try rephrasing or asking for a top-N.",
    "SQL_INVALID_COLUMN": "That request refers to a column I couldn't find. Check the column names.",
    "SQL_GROUP_BY": "I couldn't build that grouping. Try rephrasing it.",
    "SQL_CONVERSION": "A column's type didn't match what the request needed.",
    "SQL_DIVIDE_BY_ZERO": "That calculation hit a division by zero.",
    "SQL_TIMEOUT": "The query took too long. Try a narrower request.",
    "UNKNOWN": "Something went wrong while running that step.",
}


@router.post("/transform")
def transform_table(payload: models.TransformPayload, authed_user_id: int = Depends(get_current_user_id)):
    require_same_user(authed_user_id, payload.userId)

    if not payload.tables:
        raise HTTPException(status_code=400, detail="At least one table id is required.")
    if payload.sessionId is None:
        raise HTTPException(
            status_code=400,
            detail="sessionId is required — sessions are created at upload time.",
        )

    usage_service.check_quota(payload.userId)  # 429 USAGE_LIMIT before any LLM spend

    trace_id = new_trace_id()
    with usage_service.metered_call(payload.userId, "transform", trace_id) as meter:
        result = _transform_impl(payload, trace_id)
        meter.count_query = True  # reached only on success
    return result


def _transform_impl(payload: models.TransformPayload, trace_id: str) -> dict:
    tables = payload.tables
    sessionId = payload.sessionId
    userId = payload.userId

    try:
        with db.transaction() as cur:
            cur.execute("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", (sessionId,))
            row = cur.fetchone()
            if row is None or row[0] != userId:
                raise HTTPException(status_code=401, detail="User Mismatch")

            placeholders = ", ".join(["%s"] * len(tables))
            cur.execute(
                f"SELECT id, name, user_id, knowledgebase, metadata "
                f"FROM table_info WHERE id IN ({placeholders})",
                tuple(tables),
            )
            tables_rows = cur.fetchall()
            if not tables_rows:
                raise HTTPException(status_code=404, detail="No matching tables found.")

            tables_info = [
                {"user_id": row[2], "knowledgebase": row[3], "metadata": row[4], "name": row[1]}
                for row in tables_rows
            ]

            previous_query_data = None
            cur.execute(
                "SELECT id, prompt, sql_query, summary, updated_columns "
                "FROM queries WHERE session_id = %s AND is_deleted = 0 "
                "ORDER BY created_at DESC",
                (sessionId,),
            )
            r = cur.fetchone()
            if r:
                previous_query_data = {
                    "id": r[0],
                    "prompt": r[1],
                    "sql_query": r[2],
                    "summary": r[3],
                    "updated_columns": r[4],
                }

            # Two AI calls (plan, then generate) — reverted from the
            # single-call experiment, which caused reliability issues.
            query_result = query_generation.get_sql_query(payload.prompt, tables_info, previous_query_data)
            trace = query_result.get("trace", {})
            log_ai_event(trace_id, "transform.generated", prompt=payload.prompt,
                         sql=query_result["sql"], plan=trace.get("plan"),
                         raw_response=trace.get("raw_response"))

            sql_query = query_result["sql"]
            query_summary = query_result["summary"]
            updated_columns = json.dumps(query_result["columns"])

            if "Requested fields not found in schema" in sql_query:
                raise HTTPException(status_code=422, detail="Requested fields not found in schema")

            cur.execute(
                "INSERT INTO queries (session_id, prompt, sql_query, summary, updated_columns) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s)",
                (sessionId, payload.prompt, sql_query, query_summary, updated_columns),
            )
            row = cur.fetchone()
            if not row:
                raise Exception("Failed to persist query object.")
            queryObjId = row[0]

            cur.execute(
                "SELECT id, prompt, sql_query, summary, updated_columns, step_type FROM queries WHERE id = %s",
                (queryObjId,),
            )

        output, last = None, None
        for attempt in range(MAX_SQL_ATTEMPTS):
            try:
                output = pipeline.run_pipeline_result(sessionId, queryObjId)
                break
            except HTTPException:
                # Revoked/missing custom DB: not an SQL problem, so don't repair —
                # just discard this step and surface the original error.
                try:
                    db.run("DELETE FROM queries WHERE id = %s", (queryObjId,))
                except Exception:
                    logger.exception("Cleanup failed for query %s", queryObjId)
                raise
            except Exception as exc:
                info = classify_sql_error(exc)
                last = (exc, info)
                log_ai_event(trace_id, "transform.execute_failed", level="error",
                             attempt=attempt, error_code=info["code"], error=str(exc),
                             prompt=payload.prompt, sql=sql_query,
                             plan=trace.get("plan"), raw_response=trace.get("raw_response"))
                if not info["retryable"] or attempt == MAX_SQL_ATTEMPTS - 1:
                    break
                try:
                    fixed = query_generation.repair_sql(
                        payload.prompt, sql_query, str(exc),
                        json.loads(updated_columns), tables_info, previous_query_data)
                    log_ai_event(trace_id, "transform.repaired", sql=fixed["sql"],
                                 raw_response=fixed["raw_response"])
                    sql_query, query_summary = fixed["sql"], fixed["summary"]
                    db.run("UPDATE queries SET sql_query = %s, summary = %s WHERE id = %s",
                           (sql_query, query_summary, queryObjId))
                except Exception:
                    logger.exception("Repair failed (trace %s)", trace_id)
                    break

        if output is None:
            try:
                db.run("DELETE FROM queries WHERE id = %s", (queryObjId,))
            except Exception:
                logger.exception("Cleanup failed for query %s", queryObjId)
            exc, info = last
            raise HTTPException(
                status_code=422 if info["code"] != "UNKNOWN" else 500,
                detail={
                    "code": info["code"],
                    "message": USER_MESSAGES.get(info["code"], USER_MESSAGES["UNKNOWN"]),
                    "retryable": info["retryable"],
                    "trace_id": trace_id,
                },
            )

        r = db.fetch(
            "SELECT id, prompt, sql_query, summary, updated_columns, step_type FROM queries WHERE id = %s",
            params=(queryObjId,))
        q = r[0]

        return {
            "sessionId": sessionId,
            "query": {
                "id": q[0],
                "prompt": q[1],
                "sql_query": q[2],
                "summary": q[3],
                "updated_columns": json.loads(q[4]) if q[4] else [],
                "step_type": q[5],
            },
            "data": output,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Transform failed for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Transform failed: {str(e)}")
