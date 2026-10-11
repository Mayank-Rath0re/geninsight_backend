"""
Creates a new transform step in an existing session's pipeline.

Session creation no longer happens here — every session is now created
synchronously at upload time (routers/datasets.py's /upload), seeded
with an initial mechanical "SELECT *" step. sessionId is therefore
always required and must already exist; this router only appends to it.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

import models
from core import db
from core.security import get_current_user_id, require_same_user
from services import pipeline, query_generation

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["transform"])


@router.post("/transform")
def transform_table(payload: models.TransformPayload, authed_user_id: int = Depends(get_current_user_id)):
    tables = payload.tables
    sessionId = payload.sessionId
    userId = payload.userId

    require_same_user(authed_user_id, userId)

    if not tables:
        raise HTTPException(status_code=400, detail="At least one table id is required.")
    if sessionId is None:
        raise HTTPException(
            status_code=400,
            detail="sessionId is required — sessions are created at upload time.",
        )

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
            q = cur.fetchone()

        try:
            output = pipeline.run_pipeline_result(sessionId, queryObjId)
        except Exception:
            try:
                db.run("DELETE FROM queries WHERE id = %s", (queryObjId,))
            except Exception:
                logger.exception("Cleanup failed for query %s after pipeline error", queryObjId)
            raise

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