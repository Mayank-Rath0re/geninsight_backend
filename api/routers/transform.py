# routers/transform.py
"""
Creates a new transform step in a session's pipeline.

NOTE: The original app.py also had a `/edit_transform` endpoint. It was an
unfinished stub — it fetched `queriesData` and `associated_tables` and then
did nothing with them (`query3 = ""`, then a bare `pass`), so it always
returned `None` / an empty 200 response. It has been removed as dead code.
If "edit an existing transform step" is a real feature, it should be
reimplemented properly (likely: regenerate the plan/SQL for that step via
services.query_generation, update the `queries` row, and re-run
services.pipeline for anything downstream) rather than resurrected as-is.

MERGE SUPPORT: queries.step_type now exists (default 'transform', see
migration_merge.sql). This router still only ever creates 'transform'
steps — 'join' steps are created exclusively by services.merge.perform_merge
via routers/merge.py. step_type is now surfaced in this endpoint's response
so the frontend can render transform vs. join steps consistently without
special-casing which endpoint produced them.
"""

import json
import logging

from fastapi import APIRouter, HTTPException

import models
from core import db
from services import pipeline, query_generation

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["transform"])


@router.post("/transform")
def transform_table(payload: models.TransformPayload):
    tables = payload.tables
    sessionId = payload.sessionId
    userId = payload.userId

    try:
        with db.transaction() as cur:

            # 1. Create session if this is the first prompt
            if sessionId is None:
                cur.execute(
                    "INSERT INTO sessions (user_id, type) OUTPUT INSERTED.session_id VALUES (%s, %s)",
                    (userId, "Transformation"),
                )
                row = cur.fetchone()
                if not row:
                    raise Exception("Failed to create session.")
                sessionId = row[0]

            # 2. Verify session belongs to this user
            cur.execute("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", (sessionId,))
            row = cur.fetchone()
            if row is None or row[0] != userId:
                raise HTTPException(status_code=401, detail="User Mismatch")

            # 3. Fetch table info for all requested tables
            placeholders = ", ".join(["%s"] * len(tables))
            table_info_query = (
                f"SELECT id, name, user_id, knowledgebase, metadata "
                f"FROM table_info WHERE id IN ({placeholders})"
            )
            cur.execute(table_info_query, tuple(tables))
            tables_rows = cur.fetchall()
            if not tables_rows:
                raise HTTPException(status_code=404, detail="No matching tables found.")

            tables_info = [
                {
                    "user_id": row[2],
                    "knowledgebase": row[3],
                    "metadata": row[4],
                    "name": row[1],
                }
                for row in tables_rows
            ]

            # 4. Fetch the most recent query in this session (if any)
            previous_query_data = None
            cur.execute(
                "SELECT TOP 1 query_id FROM session_queries WHERE session_id = %s ORDER BY date_created DESC",
                (sessionId,),
            )
            last = cur.fetchone()
            if last:
                cur.execute(
                    "SELECT id, prompt, sql_query, summary, updated_columns FROM queries WHERE id = %s",
                    (last[0],),
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

            # 5. Generate SQL — returns dict {"sql": ..., "summary": ..., "columns": [...]}
            query_result = query_generation.get_sql_query(
                payload.prompt,
                tables_info,
                previous_query_data,
            )

            sql_query = query_result["sql"]
            query_summary = query_result["summary"]
            updated_columns = json.dumps(query_result["columns"])

            # 6. Reject before any writes if schema mismatch detected
            if "Requested fields not found in schema" in sql_query:
                raise HTTPException(status_code=422, detail="Requested fields not found in schema")

            # 7. Persist the query object (step_type defaults to 'transform' in the DB)
            cur.execute(
                "INSERT INTO queries (prompt, sql_query, summary, updated_columns) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s)",
                (payload.prompt, sql_query, query_summary, updated_columns),
            )
            row = cur.fetchone()
            if not row:
                raise Exception("Failed to persist query object.")
            queryObjId = row[0]

            # 8. Link query to its source tables in query_tables
            for table in tables:
                cur.execute(
                    "INSERT INTO query_tables (query_id, table_id) VALUES (%s, %s)",
                    (queryObjId, table),
                )

            # 9. Link query to the session in session_queries
            cur.execute(
                "INSERT INTO session_queries (session_id, query_id) VALUES (%s, %s)",
                (sessionId, queryObjId),
            )

            # 10. Read back the persisted query for the response (inside
            #     transaction while cur is open)
            cur.execute(
                "SELECT id, prompt, sql_query, summary, updated_columns, step_type FROM queries WHERE id = %s",
                (queryObjId,),
            )
            q = cur.fetchone()

        # transaction commits here — all locks released before pipeline runs

        # 11. Run the pipeline after commit so assemble_cte_query can read
        #     session_queries without deadlock
        try:
            output = pipeline.run_pipeline_result(sessionId, queryObjId)
        except Exception:
            # Pipeline failed — clean up committed rows in reverse dependency order
            try:
                db.run("DELETE FROM session_queries WHERE query_id = %s", (queryObjId,))
                db.run("DELETE FROM query_tables  WHERE query_id = %s", (queryObjId,))
                db.run("DELETE FROM queries        WHERE id = %s", (queryObjId,))
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