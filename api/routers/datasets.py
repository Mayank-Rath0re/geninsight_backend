# routers/datasets.py
"""Dataset lifecycle: upload/ingest, list, and preview (raw or 'original')."""

import csv
import logging
import os

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from core import db
from core.config import UPLOAD_DIR
from services import ingestion

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["datasets"])


# ─────────────────────────────────────────────────────────────────────────
# LIST
# ─────────────────────────────────────────────────────────────────────────

@router.get("/user_datasets")
def get_user_datasets(userId: int) -> list:
    try:
        query = (
            "SELECT id, name, user_id, knowledgebase, metadata, createdAt "
            "FROM table_info WHERE user_id = %s"
        )
        output = db.fetch(query, params=(userId,))
        if not output:
            return []

        return [
            {
                "id": row[0],
                "name": row[1],
                "user_id": row[2],
                "knowledgebase": row[3],
                "metadata": row[4],
                "createdAt": row[5].isoformat() if row[5] else None,
            }
            for row in output
        ]
    except Exception as e:
        logger.exception("Failed to fetch datasets for user %s", userId)
        raise HTTPException(status_code=500, detail=f"Failed to fetch datasets: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────
# UPLOAD
# ─────────────────────────────────────────────────────────────────────────

def _cast(value: str, col: str, sample_rows: list) -> object:
    """Cast a CSV string value to the correct Python type based on column samples."""
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
    return value  # leave as string (NVARCHAR)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    userId: int,
    apply_basic_transformation: bool = False,
    file: UploadFile = File(...),
):
    # NOTE: /upload stays multipart/form-data (required for file upload), so
    # userId / apply_basic_transformation remain query params here — this is
    # unavoidable with multipart requests and works fine through the tunnel
    # since the Dart client already sends this one correctly via
    # MultipartRequest with query params in the URL.
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["csv", "xlsx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only csv and xlsx files are allowed."
        )

    file_path = os.path.join(UPLOAD_DIR, file.filename)
    table_name = (f"user{userId}_" + file.filename).replace(".", "_").replace("@", "_")

    try:
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)

        # 1. Load dataframe and sanitize columns — always, before anything else
        df = ingestion.get_dataframe(file_path)
        df = ingestion.sanitize_columns(df)

        # 2. Optional basic transformation (its own column sanitization is now
        #    redundant but harmless since the columns are already clean)
        if apply_basic_transformation:
            df = ingestion.basic_transform(df)

        # 3. Write sanitized CSV back to disk so schema generation and the
        #    bulk insert both see clean column names
        df.to_csv(file_path, index=False)

        # 4. Build metadata & knowledge base from the sanitized df
        metadata = ingestion.extract_metadata(file_path)
        roles = ingestion.classify_column_roles(df, metadata)
        print("=== CALLING AI: Building Knowledge Base")
        kb = ingestion.build_knowledge_base(df.head(5), metadata, table_name, roles)
        del df

        # ── Single transaction: schema + data insert + table_info insert ──
        conn = db.get_connection()
        try:
            cursor = conn.cursor()

            # 5. Verify the user exists (FK constraint would 500 otherwise)
            cursor.execute("SELECT id FROM auth WHERE id = %s", (userId,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail=f"User {userId} not found")

            # 6. Create the data table from the sanitized file
            schema = db.generate_sql_schema(table_name, file_path)
            logger.info(f"SCHEMA SQL: \n{schema}")
            cursor.execute(schema)

            # 7. Bulk-insert CSV rows into the new table
            with open(file_path, newline='', encoding='utf-8') as csvfile:
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

            # 8. Insert table_info metadata row
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


# ─────────────────────────────────────────────────────────────────────────
# TABLE PREVIEW (shared helper — backs both "select existing dataset"
# preview and the "View Original" feature on transformation history)
# ─────────────────────────────────────────────────────────────────────────

def _resolve_table_name(table_id: int) -> str:
    """
    Looks up the physical table name for a table_info.id. Raises
    HTTPException(404) if no such table exists. Shared by /table_preview
    and /view_original so both features resolve names identically.
    """
    name = db.get_single_value(
        "SELECT name FROM table_info WHERE id = %s", (table_id,)
    )
    if not name:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")
    return name


def _fetch_table_preview(table_id: int, limit: int = 200) -> dict:
    """
    Runs `SELECT * FROM [<resolved table name>]` (bounded by `limit`) and
    returns {"headers": [...], "rows": [[...], ...]}.

    This is intentionally the single implementation used by both:
      - GET /table_preview   (staging an existing dataset)
      - GET /view_original   ("View Original" on a transformation step)
    so the two features can never drift apart.

    table_name comes from table_info (server-controlled, not user input),
    so it's safe to interpolate into the SQL identifier position — the
    limit is still applied via SELECT TOP, not string-formatted into the
    WHERE/value position.
    """
    table_name = _resolve_table_name(table_id)

    if limit < 0:
        raise HTTPException(status_code=400, detail="limit must be a non-negative integer")

    try:
        # NOTE: deliberately NOT using db.fetch_with_columns(limit=...)
        # here. That helper appends "OFFSET ... ROWS FETCH NEXT ... ROWS
        # ONLY", which SQL Server requires an ORDER BY immediately before —
        # and this table has no reliable, generically-known ordering
        # column (uploaded tables sometimes have an `id` PK, sometimes an
        # arbitrary `*_id` column; see db.generate_sql_schema). Using
        # OFFSET/FETCH without ORDER BY is a hard SQL syntax error.
        # `SELECT TOP` needs no ordering, so it's used directly instead.
        result = db.fetch_with_columns(f"SELECT TOP {int(limit)} * FROM [{table_name}]")
    except Exception as e:
        logger.exception("Failed to fetch preview for table_id %s (%s)", table_id, table_name)
        raise HTTPException(status_code=500, detail=f"Failed to fetch table data: {str(e)}")

    return {"headers": result["columns"], "rows": result["rows"]}


@router.get("/table_preview")
def table_preview(table_id: int, limit: int = 200):
    """
    Bounded preview of an already-uploaded table, independent of any
    session/query context. Used when a user stages an existing dataset
    (rather than a fresh local upload), which has no local bytes to
    preview client-side.
    """
    data = _fetch_table_preview(table_id, limit=limit)
    return {"table_id": table_id, "headers": data["headers"], "rows": data["rows"]}


@router.get("/view_original")
def view_original(table_id: int, limit: int = 200):
    """
    Returns the original, untransformed data for a table — i.e. what the
    table looked like before any transformation prompts were applied.
    This is the "View Original" counterpart to the historical-step
    preview (/pipeline_result): where /pipeline_result re-runs the CTE
    chain up to a given query, /view_original bypasses the pipeline
    entirely and reads the source table directly.

    Reuses the same underlying SELECT * fetch as /table_preview so both
    features are guaranteed to return identically-shaped, identically-
    sourced data.
    """
    data = _fetch_table_preview(table_id, limit=limit)
    return {"table_id": table_id, "headers": data["headers"], "rows": data["rows"]}
