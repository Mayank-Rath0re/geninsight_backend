"""
Shared "ingest a file into a new table_info row" step, used by both
routers/datasets.py's /upload and routers/merge.py's /merge_upload — the
one previous duplication point between those two flows.

Session creation now happens here too: every uploaded table gets its own
brand-new session immediately, seeded with a single, purely mechanical
"SELECT * FROM [table]" initial step (0 AI calls — the row count/columns
for that step are read directly from the table, not generated). This
replaces the old lazy "session is created on first /transform call" and
"_bootstrapSessionForMerge" bootstrap pattern.
"""

import csv
import json
import logging
import os
import re
import uuid

from fastapi import HTTPException, UploadFile, status

from core import db
from core.config import UPLOAD_DIR
from services import ingestion

logger = logging.getLogger("my_global_app_logger")

MAX_TABLE_NAME_LEN = 120


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


def _unique_table_name(base: str) -> str:
    """Same filename uploaded twice must not append rows into the first table."""
    candidate = base[:MAX_TABLE_NAME_LEN]
    suffix = 2
    while db.get_single_value("SELECT id FROM table_info WHERE name = %s", (candidate,)) is not None:
        tail = f"_{suffix}"
        candidate = base[: MAX_TABLE_NAME_LEN - len(tail)] + tail
        suffix += 1
    return candidate


def _table_name_for(user_id: int, filename: str) -> str:
    # Filenames are user input and end up inside SQL identifiers/string
    # literals (generate_sql_schema), so keep only [A-Za-z0-9_].
    stem = os.path.splitext(filename)[0]
    safe_stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_") or "dataset"
    return _unique_table_name(f"user{user_id}_{safe_stem}")


async def ingest_table_and_session(
    user_id: int,
    file: UploadFile,
    apply_basic_transformation: bool = False,
) -> dict:
    """
    Ingests `file` into a new table_info row, then creates a brand-new
    session rooted on that table with a single mechanical initial step
    (SELECT * FROM [table], no AI call — the columns for this step are
    just the table's own column list).

    Returns {"table_id", "table_name", "session_id", "session_title", "query_id"}.
    """
    # The client controls the filename: strip any directory component so a name
    # like "../../x.csv" can never escape UPLOAD_DIR.
    filename = os.path.basename((file.filename or "").replace("\\", "/"))
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ["csv", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only csv and xlsx files are allowed.",
        )

    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{filename}")
    table_name = _table_name_for(user_id, filename)

    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    try:
        df = ingestion.get_dataframe(file_path)
    except Exception:
        logger.exception("Could not parse uploaded file %s", filename)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_FILE", "message": "That file couldn't be read as a table. Check the format and try again.", "retryable": False},
        )
    if df is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNSUPPORTED_FILE", "message": "Only CSV and XLSX files are supported.", "retryable": False},
        )
    df = ingestion.sanitize_columns(df)
    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_DATASET", "message": "That file has no data rows to analyze.", "retryable": False},
        )
    if apply_basic_transformation:
        df = ingestion.basic_transform(df)
    if df.shape[1] == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "EMPTY_SCHEMA",
                "message": "Automatic cleanup would remove every column. Try uploading without clean-up.",
                "retryable": False,
            },
        )
    df.to_csv(file_path, index=False)

    metadata = ingestion.extract_metadata(file_path)
    roles = ingestion.classify_column_roles(df, metadata)
    columns = list(df.columns)
    # Full dataframe: the knowledge base's row_count/statistics must describe the
    # whole dataset (it samples head(5) internally for the prompt).
    kb_result = ingestion.build_knowledge_base(df, metadata, table_name, roles)
    kb = kb_result["knowledge_base"]
    session_title = kb_result["session_title"]
    del df

    conn = db.get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM auth WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found.")

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

        # Brand-new session rooted on this table, created synchronously as
        # part of upload (not lazily on first prompt).
        cursor.execute(
            "INSERT INTO sessions (user_id, session_title, table_id) "
            "OUTPUT INSERTED.session_id VALUES (%s, %s, %s)",
            (user_id, session_title, new_table_id),
        )
        row = cursor.fetchone()
        new_session_id = row[0]

        # Mechanical initial step — no AI call. sql_query is the literal
        # SELECT * (pipeline.py substitutes nothing for the first step,
        # since it has no {prev}), and updated_columns is just the
        # table's own column list, read directly rather than generated.
        select_sql = f"SELECT * FROM [{table_name}]"
        cursor.execute(
            "INSERT INTO queries (session_id, prompt, sql_query, summary, updated_columns, step_type) "
            "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s, %s)",
            (
                new_session_id,
                "Load data",
                select_sql,
                f"Loaded {filename}",
                json.dumps(columns),
                "transform",
            ),
        )
        row = cursor.fetchone()
        new_query_id = row[0]

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        logger.exception("Table ingestion failed for user %s (table %s)", user_id, table_name)
        raise
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()

    return {
        "table_id": new_table_id,
        "table_name": table_name,
        "session_id": new_session_id,
        "session_title": session_title,
        "query_id": new_query_id,
    }