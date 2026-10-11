"""Dataset lifecycle: upload/ingest (now creates the session too), list, and preview."""

import json
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from core import db
from core.security import get_current_user_id, require_same_user
from services.table_ingest import ingest_table_and_session

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["datasets"])


@router.get("/user_datasets")
def get_user_datasets(userId: int, authed_user_id: int = Depends(get_current_user_id)) -> list:
    require_same_user(authed_user_id, userId)
    try:
        query = "SELECT id, name, createdAt FROM table_info WHERE user_id = %s"
        output = db.fetch(query, params=(userId,))
        if not output:
            return []
        return [
            {"id": row[0], "name": row[1], "createdAt": row[2].isoformat() if row[2] else None}
            for row in output
        ]
    except Exception:
        logger.exception("Failed to fetch datasets for user %s", userId)
        raise HTTPException(status_code=500, detail="Failed to fetch datasets!")


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    userId: int,
    apply_basic_transformation: bool = False,
    file: UploadFile = File(...),
    authed_user_id: int = Depends(get_current_user_id),
):
    """
    Ingests the file, creates its table_info row, AND creates a
    brand-new session for it with a mechanical initial "SELECT *" step —
    all in one call. This replaces the old flow where /upload only
    created the table and a session was minted lazily later (either on
    first /transform, or via the frontend's silent merge-bootstrap
    transform). Uses 1 AI call total (knowledge base + session_title).
    """
    require_same_user(authed_user_id, userId)
    try:
        result = await ingest_table_and_session(
            user_id=userId,
            file=file,
            apply_basic_transformation=apply_basic_transformation,
        )
        return {
            "table_id": result["table_id"],
            "session_id": result["session_id"],
            "session_title": result["session_title"],
            "query_id": result["query_id"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Upload failed for user %s, file %s", userId, file.filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while processing the dataset: {str(e)}",
        )
    finally:
        await file.close()


@router.post("/create_session_for_table")
def create_session_for_table(userId: int, tableId: int, authed_user_id: int = Depends(get_current_user_id)):
    """
    Starts a brand-new session for an already-ingested table (the
    "select an existing dataset" path in Step 0). No AI call — the
    table's knowledge base was already generated when it was first
    uploaded, so this only needs a mechanical initial "SELECT *" step,
    exactly like a fresh upload's session creation but skipping
    ingestion entirely.
    """
    require_same_user(authed_user_id, userId)
    owner = db.get_single_value("SELECT user_id FROM table_info WHERE id = %s", (tableId,))
    if owner is None:
        raise HTTPException(status_code=404, detail=f"Table {tableId} not found")
    if owner != userId:
        raise HTTPException(status_code=401, detail="User Mismatch")

    table_name = _resolve_table_name(tableId)

    try:
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO sessions (user_id, session_title, table_id) "
                "OUTPUT INSERTED.session_id VALUES (%s, %s, %s)",
                (userId, f"Analysis of {table_name}", tableId),
            )
            row = cur.fetchone()
            new_session_id = row[0]

            select_sql = f"SELECT * FROM [{table_name}]"
            cur.execute(f"SELECT TOP 0 * FROM [{table_name}]")
            columns = [desc[0] for desc in cur.description]

            cur.execute(
                "INSERT INTO queries (session_id, prompt, sql_query, summary, updated_columns, step_type) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    new_session_id,
                    "Load data",
                    select_sql,
                    f"Loaded {table_name}",
                    json.dumps(columns),
                    "transform",
                ),
            )
            row = cur.fetchone()
            new_query_id = row[0]

        return {
            "session_id": new_session_id,
            "query_id": new_query_id,
            "sql_query": select_sql,
            "table_id": tableId,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("create_session_for_table failed for user %s, table %s", userId, tableId)
        raise HTTPException(status_code=500, detail=f"Failed to create session: {str(e)}")


def _resolve_table_name(table_id: int) -> str:
    name = db.get_single_value("SELECT name FROM table_info WHERE id = %s", (table_id,))
    if not name:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")
    return name


def _fetch_table_preview(table_id: int, limit: int = 200, offset: int = 0) -> dict:
    """
    Runs SELECT * FROM [<resolved table name>] (bounded by `limit`), plus
    a cheap COUNT(*) so the caller gets an exact total instead of the old
    "rows.length >= limit" truncation guess on the client. table_name
    comes from table_info (server-controlled), so it's safe to
    interpolate into the identifier position; limit uses SELECT TOP, not
    string-formatted into a value position.
    """
    table_name = _resolve_table_name(table_id)

    if limit < 0:
        raise HTTPException(status_code=400, detail="limit must be a non-negative integer")

    try:
        rows = db.fetch(f"SELECT TOP {limit + offset} * FROM [{table_name}]")
        total_count = db.get_single_value(f"SELECT COUNT(*) FROM [{table_name}]")
        result = {
            "columns": db.fetch_with_columns(f"SELECT TOP 0 * FROM [{table_name}]")["columns"],
            "rows": rows[offset:],
            "total_count": int(total_count) if total_count is not None else None,
        }
    except Exception as e:
        logger.exception("Failed to fetch preview for table_id %s (%s)", table_id, table_name)
        raise HTTPException(status_code=500, detail=f"Failed to fetch table data: {str(e)}")

    return {"headers": result["columns"], "rows": result["rows"], "total_count": result["total_count"]}


def _check_table_owner(table_id: int, authed_user_id: int) -> None:
    owner = db.get_single_value("SELECT user_id FROM table_info WHERE id = %s", (table_id,))
    if owner is None:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found")
    if owner != authed_user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")


@router.get("/table_preview")
def table_preview(table_id: int, limit: int = 200, authed_user_id: int = Depends(get_current_user_id)):
    _check_table_owner(table_id, authed_user_id)
    data = _fetch_table_preview(table_id, limit=limit)
    return {
        "table_id": table_id,
        "headers": data["headers"],
        "rows": data["rows"],
        "total_count": data["total_count"],
    }


@router.get("/view_original")
def view_original(table_id: int, limit: int = 200, offset: int = 0, authed_user_id: int = Depends(get_current_user_id)):
    """"View Original" — bypasses the pipeline entirely and reads the source table directly."""
    _check_table_owner(table_id, authed_user_id)
    data = _fetch_table_preview(table_id, limit=limit, offset=offset)
    return {
        "table_id": table_id,
        "headers": data["headers"],
        "rows": data["rows"],
        "total_count": data["total_count"],
    }