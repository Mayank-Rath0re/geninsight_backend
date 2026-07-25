# app.py

from fastapi import FastAPI, HTTPException, File, UploadFile, status
import db_handler
import models
import os
import logging
import transform_handler
import helper
import dashboard_handler
import json
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("my_global_app_logger")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or restrict to your frontend's origin, e.g. ["http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def check_health():
    return {"status": "healthy", "database": "connected"}


# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/login")
def login_user(email: str, password: str):
    try:
        if not db_handler.check_login_credentials(email, password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # auth.id is the real userId — table_info/sessions/dashboards all FK to it.
        query = "SELECT id, email, name FROM auth WHERE email = %s"
        row = db_handler.run_fetch_query(query, params=(email,))
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        user = row[0]
        return {
            "success": True,
            "userId": user[0],
            "name":   user[2],
            "email":  user[1],
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Login failed for user %s", email)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/signup")
def signin_user(email: str, name: str, password: str):
    try:
        query = (
            "INSERT INTO auth (email, name, password) "
            "OUTPUT INSERTED.id, INSERTED.name, INSERTED.email "
            "VALUES (%s, %s, %s)"
        )
        row = db_handler.run_insert_query(query, (email, name, password))
        if not row:
            raise Exception("Failed to retrieve inserted user row.")

        return {
            "userId": row[0],
            "name":   row[1],
            "email":  row[2],
        }
    except Exception as e:
        logger.exception("Signup failed for user %s", email)
        raise HTTPException(status_code=500, detail=f"Signup failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

UPLOAD_DIR = "./uploaded_datasets"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/user_datasets")
def get_user_datasets(userId: int) -> list:
    try:
        query = "SELECT * FROM table_info WHERE user_id = %s"
        output = db_handler.run_fetch_query(query, params=(userId,))
        if not output:
            return []
        return output
    except Exception as e:
        logger.exception("Failed to fetch datasets for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch datasets: {str(e)}")


@app.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(userId: int, apply_basic_transformation: bool = False, file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["csv", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only csv and xlsx files are allowed."
        )

    file_path  = os.path.join(UPLOAD_DIR, file.filename)
    table_name = (f"user{userId}_" + file.filename).replace(".", "_").replace("@", "_")

    try:
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)

        # 1. Load dataframe and sanitize columns — always, before anything else
        df = helper.get_dataframe(file_path)
        df.columns = (
            df.columns
            .str.strip()
            .str.replace(r'[^A-Za-z0-9]+', '_', regex=True)
            .str.strip('_')
        )

        # 2. Optional basic transformation (its own column sanitization is now redundant
        #    but harmless since the columns are already clean)
        if apply_basic_transformation:
            df = transform_handler.basic_transform(df)

        # 3. Write sanitized CSV back to disk so generate_sql_schema and the
        #    bulk insert both see clean column names
        df.to_csv(file_path, index=False)

        # 4. Build metadata & knowledge base from the sanitized df
        metadata = transform_handler.extract_metadata(file_path)
        roles    = transform_handler.classify_column_roles(df, metadata)
        print("=== CALLING AI: Building Knowledge Base")
        kb       = transform_handler.build_knowledge_base(df.head(5), metadata, table_name, roles)
        del df

        # ── Single transaction: schema + data insert + table_info insert ──
        conn = db_handler.get_db_connection()
        try:
            cursor = conn.cursor()

            # 5. Verify the user exists (FK constraint would 500 otherwise; fail cleanly instead)
            cursor.execute("SELECT id FROM auth WHERE id = %s", (userId,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail=f"User {userId} not found")

            # 6. Create the data table from the sanitized file
            schema = db_handler.generate_sql_schema(table_name, file_path)
            logger.info(f"SCHEMA SQL: \n{schema}")
            cursor.execute(schema)

            # 7. Bulk-insert CSV rows into the new table
            import csv

            def _cast(value: str, col: str, sample_rows: list) -> object:
                """Cast a CSV string value to the correct Python type based on column samples."""
                if value == "" or value is None:
                    return None
                # Determine type from first non-empty sample in the column
                sample = next((r[col] for r in sample_rows if r[col] != ""), "")
                try:
                    int(sample)
                    return int(value)
                except (ValueError, TypeError):
                    pass
                try:
                    float(sample)
                    return float(value)
                except (ValueError, TypeError):
                    pass
                return value  # leave as string (NVARCHAR)

            with open(file_path, newline='', encoding='utf-8') as csvfile:
                reader     = csv.DictReader(csvfile)
                rows       = list(reader)
                if rows:
                    cols         = list(rows[0].keys())
                    col_list     = ", ".join(f"[{c}]" for c in cols)
                    placeholders = ", ".join(["%s"] * len(cols))
                    insert_sql   = f"INSERT INTO [{table_name}] ({col_list}) VALUES ({placeholders})"
                    for row in rows:
                        typed_row = tuple(_cast(row[c], c, rows[:10]) for c in cols)
                        cursor.execute(insert_sql, typed_row)

            # 8. Insert table_info metadata row (user_id, not user_email)
            cursor.execute(
                "INSERT INTO table_info (name, user_id, knowledgebase, metadata, createdAt) "
                "VALUES (%s, %s, %s, %s, SYSDATETIME())",
                (table_name, userId, kb, metadata)
            )

            # 9. Fetch the new ID inside the same transaction
            cursor.execute("SELECT id FROM table_info WHERE name = %s", (table_name,))
            row = cursor.fetchone()
            if not row:
                raise Exception("Failed to retrieve ID after table_info insert.")
            new_id = row[0]

            conn.commit()

        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

        return new_id

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Upload failed for user %s, file %s", userId, file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while processing the dataset: {str(e)}"
        )
    finally:
        await file.close()


# SESSION

@app.get("/get_session")
def session_info(sessionId: int, userId: int):
    try:
        # get session data
        query1 = "SELECT TOP 1 * FROM sessions WHERE session_id = %s"
        params1 = (sessionId,)
        output = db_handler.run_fetch_query(query1, params=params1)
        if not output:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unable to fetch session for session id: {sessionId}"
            )
        # match session.user_id with input userId (fallback on not matching)
        if output[0][1] != userId:
            raise HTTPException(
                status_code=401,
                detail="User Mismatch"
            )
        # fetch session history - queries
        query2 = "SELECT * FROM session_queries WHERE session_id = %s"
        params2 = (sessionId,)
        output2 = db_handler.run_fetch_query(query2, params=params2)
        if not output2:
            return {
                "session_id": sessionId,
                "query_history": [],
                "tables": []
            }

        query_ids = [row[1] for row in output2]
        query_id_placeholders = ", ".join(["%s"] * len(query_ids))

        # get table id and table name
        query3 = f"SELECT * FROM query_tables WHERE query_id IN ({query_id_placeholders})"
        output3 = db_handler.run_fetch_query(query3, params=tuple(query_ids))
        if not output3:
            raise HTTPException(
                status_code=404,
                detail="Error fetching tables for history"
            )

        table_ids = list(set(table[1] for table in output3))
        table_id_placeholders = ", ".join(["%s"] * len(table_ids))

        query4 = f"SELECT id, name FROM table_info WHERE id IN ({table_id_placeholders})"
        output4 = db_handler.run_fetch_query(query4, params=tuple(table_ids))
        if not output4:
            raise HTTPException(
                status_code=404,
                detail="Error: Empty Table Info"
            )

        # get queries info
        query5 = f"SELECT * FROM queries WHERE id IN ({query_id_placeholders})"
        output5 = db_handler.run_fetch_query(query5, params=tuple(query_ids))
        if not output5:
            raise HTTPException(
                status_code=404,
                detail="error fetching transform history"
            )

        # return final json
        # Build lookup maps
        table_map = {row[0]: row[1] for row in output4}  # {table_id: table_name}

        # {query_id: [table_ids]} from query_tables
        query_table_map: dict = {}
        for row in output3:
            qid, tid = row[0], row[1]
            query_table_map.setdefault(qid, []).append(tid)

        # Build query history in pipeline order (date_created ASC)
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

            query_history.append({
                "id":              query_row[0],
                "prompt":          query_row[1],
                "sql_query":       query_row[2],
                "summary":         query_row[3],
                "updated_columns": json.loads(query_row[4]) if query_row[4] else [],
                "tables":          associated_tables,
                "date_created":    sq[2].isoformat() if sq[2] else None,
                "date_modified":   sq[3].isoformat() if sq[3] else None,
            })

        # Unique tables across the entire session
        session_tables = [
            {"id": tid, "name": name}
            for tid, name in table_map.items()
        ]

        return {
            "session_id":    sessionId,
            "query_history": query_history,
            "tables":        session_tables,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch session %s", sessionId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch session: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# TRANSFORM
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/transform")
def transform_table(payload: models.TransformPayload):
    tables    = payload.tables
    sessionId = payload.sessionId
    userId    = payload.userId

    try:
        with db_handler.transaction() as cur:

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
            placeholders     = ", ".join(["%s"] * len(tables))
            table_info_query = (
                f"SELECT id, name, user_id, knowledgebase, metadata "
                f"FROM table_info WHERE id IN ({placeholders})"
            )
            cur.execute(table_info_query, tuple(tables))
            tables_rows = cur.fetchall()
            if not tables_rows:
                raise HTTPException(status_code=404, detail="No matching tables found.")

            # Build {name: knowledgebase} dict for parse_query_intent
            tables_data = {row[1]: row[3] for row in tables_rows}

            tables_info = [
                {
                    "user_id":       row[2],
                    "knowledgebase": row[3],   
                    "metadata":      row[4],
                    "name":          row[1],
                }
                for row in tables_rows
            ]

            # 5. Fetch the most recent query in this session (if any)
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
                        "id":              r[0],
                        "prompt":          r[1],
                        "sql_query":       r[2],
                        "summary":         r[3],
                        "updated_columns": r[4],
                    }

            # 6. Generate SQL — returns dict {"sql": ..., "summary": ..., "columns": [...]}
            query_result = transform_handler.get_sql_query(
                payload.prompt,
                tables_info,
                previous_query_data,
            )

            sql_query       = query_result["sql"]
            query_summary   = query_result["summary"]
            updated_columns = json.dumps(query_result["columns"])  # store as JSON string

            # 7. Reject before any writes if schema mismatch detected
            if "Requested fields not found in schema" in sql_query:
                raise HTTPException(status_code=422, detail="Requested fields not found in schema")

            # 8. Persist the query object
            cur.execute(
                "INSERT INTO queries (prompt, sql_query, summary, updated_columns) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s)",
                (payload.prompt, sql_query, query_summary, updated_columns),
            )
            row = cur.fetchone()
            if not row:
                raise Exception("Failed to persist query object.")
            queryObjId = row[0]

            # 9. Link query to its source tables in query_tables
            for table in tables:
                cur.execute(
                    "INSERT INTO query_tables (query_id, table_id) VALUES (%s, %s)",
                    (queryObjId, table),
                )

            # 10. Link query to the session in session_queries
            cur.execute(
                "INSERT INTO session_queries (session_id, query_id) VALUES (%s, %s)",
                (sessionId, queryObjId),
            )

            # 11. Read back the persisted query for the response (inside transaction while cur is open)
            cur.execute(
                "SELECT id, prompt, sql_query, summary, updated_columns FROM queries WHERE id = %s",
                (queryObjId,),
            )
            q = cur.fetchone()

        # transaction commits here — all locks released before pipeline runs

        # 12. Run the pipeline after commit so assemble_cte_query can read session_queries without deadlock
        try:
            output = transform_handler.run_pipeline_result(sessionId, queryObjId)
        except Exception as e:
            # Pipeline failed — clean up committed rows in reverse dependency order
            try:
                db_handler.run_query("DELETE FROM session_queries WHERE query_id = %s", (queryObjId,))
                db_handler.run_query("DELETE FROM query_tables  WHERE query_id = %s", (queryObjId,))
                db_handler.run_query("DELETE FROM queries        WHERE id = %s",       (queryObjId,))
            except Exception:
                logger.exception("Cleanup failed for query %s after pipeline error", queryObjId)
            raise

        return {
            "sessionId": sessionId,
            "query": {
                "id":              q[0],
                "prompt":          q[1],
                "sql_query":       q[2],
                "summary":         q[3],
                "updated_columns": json.loads(q[4]) if q[4] else [],
            },
            "data": output,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Transform failed for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Transform failed: {str(e)}")

# ─────────────────────────────────────────────────────────────────────────────
# EDIT / ROLLBACK (stubs)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/edit_transform")
def edit_transform(payload: models.TransformPayload, queryId: int):
    # get query information
    query1 = "SELECT * FROM queries WHERE id = %s"
    params1 = (queryId,)
    queriesData = db_handler.run_fetch_query(query1, params1)
    query2 = "SELECT * FROM query_tables WHERE query_id = %s"
    params2 = (queryId,)
    associated_tables = db_handler.run_fetch_query(query2, params2)
    # get previous query summary
    query3 = ""
    # redefine query intent
    # regenerate base sql query
    # re-run the pipeline
    # send the updated information
    pass


@app.post("/preview")
def preview(sessionId: int, queryId: int):
    pass


@app.post("/rollback_transform")
def rollback_transform(sessionId: int, userId: int, queryId: int):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/dashboard_intent_basic")
def dashboard_intent_basic(userId: int, table_id: int, prompt: str):
    try:
        # fetch table info
        q = "SELECT metadata, knowledgebase FROM table_info WHERE id = %s"
        row = db_handler.run_fetch_query(q, params=(table_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Table not found.")
        metadata       = json.loads(row[0][0])
        knowledge_base = json.loads(row[0][1])

        statement  = "SELECT name FROM table_info WHERE id = %s"
        table_name = db_handler.get_single_value_db(statement, (table_id,))
        if not table_name:
            raise HTTPException(status_code=404, detail="Table name not found.")

        # build dashboard query intent
        dashboard_spec_raw = dashboard_handler.extract_dashboard_intent(table_id, prompt, knowledge_base, metadata, table_name)
        dashboard_spec     = json.dumps(dashboard_spec_raw)

        # dashboards requires user_id and table_id (table_id is NOT NULL in schema)
        statement       = (
            "INSERT INTO dashboards (user_id, table_id, dashboard_intent) "
            "OUTPUT INSERTED.dashboard_id VALUES (%s, %s, %s)"
        )
        dashboardparams = (userId, table_id, dashboard_spec)
        dashboard_obj_id = db_handler.run_insert_query(statement, dashboardparams)

        # show_dashboard_spec
        showDashboardIntent = dashboard_handler.render_dashboard_summary(dashboard_spec_raw)
        return {
            "dashboard_id":     dashboard_obj_id,
            "dashboard_intent": showDashboardIntent
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Dashboard intent failed for table %s", table_id)
        raise HTTPException(status_code=500, detail=f"Dashboard intent failed: {str(e)}")


@app.post("/cross_questioning")
def cross_questioning(dashboard_id: int, update_prompt: str):
    try:
        user_feedback = dashboard_handler.collect_feedback(update_prompt)
        statement = "UPDATE dashboards SET user_response = %s WHERE dashboard_id = %s"
        params    = (user_feedback, dashboard_id)
        db_handler.run_query(statement, params)

        q   = "SELECT dashboard_intent, table_id FROM dashboards WHERE dashboard_id = %s"
        row = db_handler.run_fetch_query(q, params=(dashboard_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Dashboard not found.")
        dashboard_intent = json.loads(row[0][0])
        table_id         = row[0][1]

        q2          = "SELECT metadata FROM table_info WHERE id = %s"
        metadata_raw = db_handler.get_single_value_db(q2, (table_id,))
        if not metadata_raw:
            raise HTTPException(status_code=404, detail="Table metadata not found.")
        metadata = json.loads(metadata_raw)

        if user_feedback not in ["skip", "confirm", "okay", "go"]:
            updated_dashboard_intent = dashboard_handler.apply_dashboard_feedback(dashboard_intent, user_feedback, metadata)
        else:
            updated_dashboard_intent = dashboard_intent

        db_handler.run_query(
            "UPDATE dashboards SET dashboard_intent = %s WHERE dashboard_id = %s",
            (json.dumps(updated_dashboard_intent), dashboard_id)
        )
        return {
            "dashboard_id":     dashboard_id,
            "dashboard_intent": updated_dashboard_intent
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Cross questioning failed for dashboard %s", dashboard_id)
        raise HTTPException(status_code=500, detail=f"Cross questioning failed: {str(e)}")
    

# ─────────────────────────────────────────────────────────────────────────────
# Additions / replacements for app.py
# ─────────────────────────────────────────────────────────────────────────────

# 1) NEW: list sessions for a user (for the "Transformations" / "History"
#    nav popups). Add near the SESSION section, after /get_session.

@app.get("/user_sessions")
def get_user_sessions(userId: int, search: str = ""):
    """
    Returns a lightweight summary of every session belonging to userId,
    most-recent first: session id, a title (first prompt in that session,
    since sessions have no name column), the step count, and last-touched
    timestamp. `search` (optional) filters by substring match against that
    title so the popup's search field has something to hit server-side.
    """
    try:
        query = "SELECT session_id, type FROM sessions WHERE user_id = %s"
        rows = db_handler.run_fetch_query(query, params=(userId,))
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
        sq_rows = db_handler.run_fetch_query(sq_query, params=tuple(session_ids)) or []

        by_session: dict = {}
        for sid, qid, created in sq_rows:
            by_session.setdefault(sid, []).append((qid, created))

        all_query_ids = list({qid for _, qid, _ in sq_rows})
        prompt_map = {}
        if all_query_ids:
            qp = ", ".join(["%s"] * len(all_query_ids))
            q_rows = db_handler.run_fetch_query(
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
                "session_id":   sid,
                "type":         session_type,
                "title":        title,
                "step_count":   len(steps),
                "last_touched": last_touched.isoformat() if last_touched else None,
            })

        # Most recently touched first; sessions with no queries yet sink to the bottom.
        results.sort(key=lambda r: r["last_touched"] or "", reverse=True)
        return results

    except Exception as e:
        logger.exception("Failed to fetch sessions for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch sessions: {str(e)}")


# 2) NEW: run the pipeline only up to a given query_id within a session.
#    Wraps transform_handler.run_pipeline_result, which already supports
#    this via assemble_cte_query's query_id slicing. Add near /transform.

@app.get("/pipeline_result")
def pipeline_result(sessionId: int, userId: int, queryId: int):
    """
    Re-runs the CTE-chained pipeline for `sessionId`, truncated to (and
    including) `queryId`. Pass queryId=-1 to run the full pipeline.
    Used by the frontend to preview any step in transformation history
    without mutating it (unlike /rollback_transform, this doesn't delete
    later steps).
    """
    try:
        # Verify session belongs to this user before running anything.
        row = db_handler.run_fetch_query(
            "SELECT TOP 1 user_id FROM sessions WHERE session_id = %s",
            params=(sessionId,),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if row[0][0] != userId:
            raise HTTPException(status_code=401, detail="User Mismatch")

        try:
            output = transform_handler.run_pipeline_result(sessionId, queryId)
        except ValueError as e:
            # assemble_cte_query raises ValueError for bad query_id / broken chain
            raise HTTPException(status_code=422, detail=str(e))

        return {
            "sessionId": sessionId,
            "queryId":   queryId,
            "data":      output,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Pipeline result failed for session %s, query %s", sessionId, queryId
        )
        raise HTTPException(status_code=500, detail=f"Pipeline result failed: {str(e)}")


# 3) REPLACE the existing rollback_transform stub with a real implementation.
#    Deletes this query and everything after it in the session (session_queries,
#    query_tables, queries), matching what the Flutter side already assumes
#    (_history.removeWhere((h) => h.id >= id)).

@app.post("/rollback_transform")
def rollback_transform(sessionId: int, userId: int, queryId: int):
    try:
        with db_handler.transaction() as cur:
            # Verify session ownership
            cur.execute("SELECT TOP 1 user_id FROM sessions WHERE session_id = %s", (sessionId,))
            row = cur.fetchone()
            if row is None or row[0] != userId:
                raise HTTPException(status_code=401, detail="User Mismatch")

            # Pipeline order for this session
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

        return {"sessionId": sessionId, "removed_query_ids": to_remove}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Rollback failed for session %s, query %s", sessionId, queryId)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")