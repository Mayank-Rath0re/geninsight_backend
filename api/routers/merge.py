# routers/merge.py
"""
Two entry points for merging a second table's data into the current
session, per product requirement:

  POST /merge_upload   — user picks "upload new file": ingests a fresh
                          CSV/XLSX exactly like /upload, wraps it in a
                          brand-new session of its own (with an initial
                          load step so it has something to merge FROM),
                          then merges that new session into the target
                          session.

  POST /merge_session   — user picks "merge from existing session":
                          same end result, skipping the upload/ingest
                          step — source_session_id is supplied directly.

Both delegate the actual join-planning/SQL-generation/persistence work to
services.merge.perform_merge, which is the single source of truth for
what "merging session B into session A" means. The source session is
NEVER mutated by either flow — it remains fully independent and usable
on its own afterward.

Response shape for both endpoints matches /transform's response shape
(sessionId / query / data), with query.step_type == "join" and an extra
query.merge block describing what was merged in — see services/merge.py.
"""

import csv
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from core import db
from core.config import UPLOAD_DIR
from services import ingestion, merge

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["merge"])


# ─────────────────────────────────────────────────────────────────────────
# Shared: ingest a file into a brand-new table_info row + a brand-new
# session with a single initial "load" step, so the resulting session is
# immediately mergeable (perform_merge requires source session to have
# >=1 step — see services/merge.py's note on this).
#
# This mirrors routers/datasets.py's /upload almost exactly. Deliberately
# NOT calling that router function directly (FastAPI route functions
# aren't meant to be called as plain Python functions with a synthetic
# UploadFile), so the table-creation logic is duplicated here in
# condensed form. If this duplication becomes a maintenance problem,
# extract routers/datasets.py's upload body into a services/ingestion.py
# function both call — flagged as a reasonable follow-up, not done here
# to keep this change scoped to merge functionality only.
# ─────────────────────────────────────────────────────────────────────────

def _cast(value: str, col: str, sample_rows: list) -> object:
    if value == "" or value is None:
        return None
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
    return value


async def _ingest_new_table_and_session(user_id: int, file: UploadFile, apply_basic_transformation: bool) -> dict:
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["csv", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only csv and xlsx files are allowed.",
        )

    file_path = os.path.join(UPLOAD_DIR, file.filename)
    table_name = (f"user{user_id}_" + file.filename).replace(".", "_").replace("@", "_")

    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    df = ingestion.get_dataframe(file_path)
    df = ingestion.sanitize_columns(df)
    if apply_basic_transformation:
        df = ingestion.basic_transform(df)
    df.to_csv(file_path, index=False)

    metadata = ingestion.extract_metadata(file_path)
    roles = ingestion.classify_column_roles(df, metadata)
    print("=== CALLING AI: Building Knowledge Base (merge-upload)")
    kb = ingestion.build_knowledge_base(df.head(5), metadata, table_name, roles)
    del df

    conn = db.get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM auth WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")

        schema = db.generate_sql_schema(table_name, file_path)
        cursor.execute(schema)

        with open(file_path, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            rows = list(reader)
            if rows:
                cols = list(rows[0].keys())
                col_list = ", ".join(f"[{c}]" for c in cols)
                placeholders = ", ".join(["%s"] * len(cols))
                insert_sql = f"INSERT INTO [{table_name}] ({col_list}) VALUES ({placeholders})"
                for row in rows:
                    typed_row = tuple(_cast(row[c], c, rows[:10]) for c in cols)
                    cursor.execute(insert_sql, typed_row)

        cursor.execute(
            "INSERT INTO table_info (name, user_id, knowledgebase, metadata, createdAt) "
            "VALUES (%s, %s, %s, %s, SYSDATETIME())",
            (table_name, user_id, kb, metadata),
        )
        cursor.execute("SELECT id FROM table_info WHERE name = %s", (table_name,))
        row = cursor.fetchone()
        if not row:
            raise Exception("Failed to retrieve ID after table_info insert.")
        new_table_id = row[0]

        # Brand-new session for this table, with a single initial "load"
        # step: SELECT * FROM [<table>]. This is what makes the new
        # session immediately mergeable (perform_merge needs >=1 step to
        # resolve {merge} against) and gives it an entry in its own right
        # in the sidebar/session history, exactly like a normal
        # first-prompt-creates-a-session flow.
        cursor.execute(
            "INSERT INTO sessions (user_id, type) OUTPUT INSERTED.session_id VALUES (%s, %s)",
            (user_id, "Transformation"),
        )
        row = cursor.fetchone()
        new_session_id = row[0]

        import json as _json
        columns = list(csv.DictReader(open(file_path, newline="", encoding="utf-8")).fieldnames or [])

        cursor.execute(
            "INSERT INTO queries (prompt, sql_query, summary, updated_columns, step_type) "
            "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s)",
            (
                f"Load {file.filename}",
                f"SELECT * FROM [{table_name}]",
                f"Initial load of {file.filename}",
                _json.dumps(columns),
                "transform",
            ),
        )
        row = cursor.fetchone()
        new_query_id = row[0]

        cursor.execute(
            "INSERT INTO query_tables (query_id, table_id) VALUES (%s, %s)",
            (new_query_id, new_table_id),
        )
        cursor.execute(
            "INSERT INTO session_queries (session_id, query_id) VALUES (%s, %s)",
            (new_session_id, new_query_id),
        )

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

    return {"table_id": new_table_id, "session_id": new_session_id, "table_name": table_name}


# ─────────────────────────────────────────────────────────────────────────
# POST /merge_upload — upload a new file, then merge it into target session
# ─────────────────────────────────────────────────────────────────────────

@router.post("/merge_upload")
async def merge_upload(
    userId: int,
    targetSessionId: int,
    apply_basic_transformation: bool = False,
    hint: str = Form(default=""),
    file: UploadFile = File(...),
):
    """
    Form/multipart: file (csv or xlsx).
    Query params: userId, targetSessionId, apply_basic_transformation.
    Form field: hint (optional free-text join guidance, e.g. "join on customer_id").

    1. Ingests `file` exactly like /upload, producing a new table_info row
       AND a brand-new session wrapping it (with an initial load step).
    2. Merges that new session into targetSessionId as a join step.

    Returns the same shape as /transform, with query.step_type == "join".
    """
    try:
        ingest_result = await _ingest_new_table_and_session(userId, file, apply_basic_transformation)
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
        # Surface the newly-created source session/table id so the
        # frontend can add it to the sidebar's session list immediately
        # without a full refetch.
        result["newSourceSessionId"] = ingest_result["session_id"]
        result["newSourceTableId"] = ingest_result["table_id"]
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "merge_upload: merge failed (target=%s, new source=%s)",
            targetSessionId, ingest_result["session_id"],
        )
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────
# POST /merge_session — merge an existing session into target session
# ─────────────────────────────────────────────────────────────────────────

class MergeSessionRequest(BaseModel):
    userId: int
    targetSessionId: int
    sourceSessionId: int
    hint: str = ""


@router.post("/merge_session")
def merge_session(payload: MergeSessionRequest):
    """
    Merges an already-existing session (sourceSessionId) into
    targetSessionId as a join step. sourceSessionId is left completely
    untouched — it remains independently usable afterward.

    Returns the same shape as /transform, with query.step_type == "join".
    """
    return merge.perform_merge(
        target_session_id=payload.targetSessionId,
        source_session_id=payload.sourceSessionId,
        user_id=payload.userId,
        user_hint=payload.hint,
    )