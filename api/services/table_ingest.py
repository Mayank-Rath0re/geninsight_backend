"""
Shared "ingest a file into a new table_info row" step, used by both
routers/datasets.py's /upload and routers/merge.py's /merge_upload — the
one previous duplication point between those two flows.

Session creation now happens here too: every uploaded table gets its own
brand-new session immediately, seeded with a single, purely mechanical
"SELECT * FROM [table]" initial step (0 AI calls — the row count/columns
for that step are read directly from the table, not generated).

The file is parsed exactly once into a DataFrame. Metadata, the SQL
schema and the rows written to the database all come from that frame —
the old path re-read the CSV from disk four separate times and inserted
one row per round trip, which is what made multi-hundred-thousand-row
uploads take minutes.
"""

import json
import logging
import os
import re
import uuid

from fastapi import HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from core import db
from core.config import UPLOAD_DIR
from services import ingestion

logger = logging.getLogger("my_global_app_logger")

MAX_TABLE_NAME_LEN = 120

# Progress phases reported to the client while an upload is processed.
PHASE_UPLOADING = "uploading"
PHASE_CLEANING = "cleaning"
PHASE_ANALYZING = "analyzing"
PHASE_IMPORTING = "importing"
PHASE_PREVIEW = "preview"

# Overall percentage each phase starts at. The client shows the same phases,
# so "uploading" (bytes in flight, measured client-side) covers 0-20%.
_MILESTONE_CLEANING = 20
_MILESTONE_ANALYZING = 34
_MILESTONE_IMPORTING = 52
_MILESTONE_PREVIEW = 92


def _report(progress, phase: str, pct: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(phase, pct, message)
    except Exception:
        # Progress reporting must never abort an upload.
        logger.debug("Upload progress callback failed", exc_info=True)


def _datetime_to_objects(df):
    """pymssql wants datetime.datetime, not numpy datetime64/Timestamp."""
    for col in df.columns:
        if df[col].dtype.kind == "M":
            df[col] = df[col].dt.to_pydatetime()
    return df


def _frame_rows(frame) -> list:
    """Chunk -> list of tuples with NaN/NaT already converted to None."""
    ready = frame.astype(object).where(frame.notna(), None)
    return [tuple(row) for row in ready.values.tolist()]


def _load_dataframe(file_path: str, filename: str):
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
    return df


def _validate_filename(filename: str) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ["csv", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only csv and xlsx files are allowed.",
        )


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


def ingest_table_and_session_from_path(
    user_id: int,
    file_path: str,
    filename: str,
    apply_basic_transformation: bool = False,
    progress=None,
) -> dict:
    """Synchronous ingest core. Runs in a worker thread — it is deliberately not
    async so the pandas/DB work never blocks the event loop.

    `progress(phase, pct, message)` is optional; pct is overall 0-100.
    """
    _report(progress, PHASE_CLEANING, _MILESTONE_CLEANING, "Reading the file")

    df = _load_dataframe(file_path, filename)
    df = ingestion.sanitize_columns(df)
    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_DATASET", "message": "That file has no data rows to analyze.", "retryable": False},
        )

    if apply_basic_transformation:
        _report(progress, PHASE_CLEANING, 26, "Removing duplicates and filling gaps")
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

    table_name = _table_name_for(user_id, filename)
    columns = list(df.columns)

    _report(progress, PHASE_ANALYZING, _MILESTONE_ANALYZING, "Profiling columns")
    metadata = ingestion.metadata_from_dataframe(df)
    roles = ingestion.classify_column_roles(df, metadata)

    # Derive the schema while datetime columns still carry their dtype.
    schema = db.generate_sql_schema(table_name, df)

    _report(progress, PHASE_ANALYZING, 44, "Describing the dataset with AI")
    # Full dataframe: the knowledge base's row_count/statistics must describe the
    # whole dataset (it sends exactly ingestion.SAMPLE_ROWS raw rows to the model).
    kb_result = ingestion.build_knowledge_base(df, metadata, table_name, roles)
    kb = kb_result["knowledge_base"]
    session_title = kb_result["session_title"]

    # Only now convert datetime64 -> datetime, so the stats block above keeps
    # real dates; the driver needs plain datetime objects for the inserts.
    _datetime_to_objects(df)

    row_count = len(df)
    inserted = 0
    last_reported_pct = -1

    conn = db.get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM auth WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found.")

        _report(progress, PHASE_IMPORTING, _MILESTONE_IMPORTING, "Creating the table")
        cursor.execute(schema)

        batch_size = db.max_rows_per_insert(len(columns))
        for start in range(0, row_count, batch_size):
            chunk = df.iloc[start:start + batch_size]
            inserted += db.insert_rows(cursor, table_name, columns, _frame_rows(chunk))
            if row_count:
                pct = _MILESTONE_IMPORTING + int(
                    (_MILESTONE_PREVIEW - 1 - _MILESTONE_IMPORTING) * inserted / row_count
                )
                if pct != last_reported_pct:
                    last_reported_pct = pct
                    _report(
                        progress, PHASE_IMPORTING, pct,
                        f"Writing {inserted:,} of {row_count:,} rows",
                    )

        _report(progress, PHASE_PREVIEW, _MILESTONE_PREVIEW, "Creating the preview")

        cursor.execute(
            "INSERT INTO table_info (name, user_id, knowledgebase, metadata, createdAt) "
            "VALUES (%s, %s, %s, %s, SYSUTCDATETIME())",
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
        _report(progress, PHASE_PREVIEW, 100, "Ready")
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
        "row_count": row_count,
    }


def save_uploaded_bytes(contents: bytes, filename: str) -> str:
    file_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{filename}")
    with open(file_path, "wb") as f:
        f.write(contents)
    return file_path


async def ingest_table_and_session(
    user_id: int,
    file: UploadFile,
    apply_basic_transformation: bool = False,
    progress=None,
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
    _validate_filename(filename)

    contents = await file.read()
    file_path = save_uploaded_bytes(contents, filename)
    return await run_in_threadpool(
        ingest_table_and_session_from_path,
        user_id,
        file_path,
        filename,
        apply_basic_transformation,
        progress,
    )
