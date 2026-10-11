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
import os

from fastapi import HTTPException, UploadFile, status

from core import db
from core.config import UPLOAD_DIR
from services import ingestion


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
    columns = list(df.columns)
    print("=== CALLING AI: Building Knowledge Base + session_title (1 call)")
    kb_result = ingestion.build_knowledge_base(df.head(5), metadata, table_name, roles)
    kb = kb_result["knowledge_base"]
    session_title = kb_result["session_title"]
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
                f"Loaded {file.filename}",
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
        raise
    finally:
        cursor.close()
        conn.close()

    return {
        "table_id": new_table_id,
        "table_name": table_name,
        "session_id": new_session_id,
        "session_title": session_title,
        "query_id": new_query_id,
    }