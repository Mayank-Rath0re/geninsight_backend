"""Full-result downloads (CSV/XLSX) for datasets and transformed pipelines.

The client only ever holds one page of rows, so exporting on the client would
silently truncate. This endpoint re-runs the source (pipeline or raw table) and
streams the complete result. Visible columns and the table's current sort order
are applied server-side so the file matches what the user sees.
"""

import csv
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core import db
from core.security import get_current_user_id
from services import pipeline
from services.custom_db import execution

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(tags=["export"])

_MIME = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _sort_key(row: list, index: int) -> tuple:
    value = row[index] if index < len(row) else None
    if value is None or value == "":
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float, Decimal)):
        return (0, float(value), "")
    text = str(value)
    try:
        return (0, float(text), "")
    except ValueError:
        return (1, 0.0, text.lower())


def _sort_rows(rows: list, index: int, ascending: bool) -> list:
    sortable = [r for r in rows if _sort_key(r, index)[0] != 2]
    blanks = [r for r in rows if _sort_key(r, index)[0] == 2]
    sortable.sort(key=lambda r: _sort_key(r, index), reverse=not ascending)
    return sortable + blanks


def _project(headers: List[str], rows: list, columns: Optional[List[str]]):
    if not columns:
        return headers, rows
    lookup = {name: i for i, name in enumerate(headers)}
    indices = []
    seen = set()
    for name in columns:
        idx = lookup.get(name)
        if idx is not None and idx not in seen:
            indices.append(idx)
            seen.add(idx)
    if not indices:
        raise HTTPException(status_code=400, detail="None of the requested columns exist in this result.")
    return (
        [headers[i] for i in indices],
        [[row[i] if i < len(row) else "" for i in indices] for row in rows],
    )


def _filename(base: str, fmt: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base or "dataset").strip(" ._") or "dataset"
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{cleaned}_{stamp}.{fmt}"


def _csv_response(headers, rows, filename) -> Response:
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel needs a BOM to read UTF-8 correctly
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type=_MIME["csv"],
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "X-Export-Rows": str(len(rows)),
        },
    )


def _xlsx_response(headers, rows, filename) -> Response:
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    sheet = book.create_sheet("Data")
    sheet.append(list(headers))
    for row in rows:
        sheet.append([_cell(v) for v in row])

    buf = io.BytesIO()
    book.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type=_MIME["xlsx"],
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "X-Export-Rows": str(len(rows)),
        },
    )


def _pipeline_data(session_id: int, query_id: int, user_id: int):
    row = db.fetch(
        "SELECT user_id, session_title FROM sessions WHERE session_id = %s AND is_deleted = 0",
        params=(session_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    if row[0][0] != user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You don't have access to this session.", "retryable": False})

    try:
        result = pipeline.run_pipeline_result(session_id, query_id)
    except ValueError as exc:
        logger.warning("Export pipeline invalid for session %s, query %s: %s", session_id, query_id, exc)
        raise HTTPException(
            status_code=422,
            detail={"code": "PIPELINE_INCOMPLETE",
                    "message": "That step's data couldn't be recomputed. Try exporting an earlier step.",
                    "retryable": False},
        )
    return result["columns"], result["rows"], row[0][1] or "dataset"


def _table_data(table_id: int, user_id: int):
    row = db.fetch(
        "SELECT user_id, name FROM table_info WHERE id = %s AND is_deleted = 0",
        params=(table_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    if row[0][0] != user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You don't have access to this dataset.", "retryable": False})

    table_name = row[0][1]
    result = execution.run_for_table(
        table_id,
        f"SELECT * FROM {execution.quote_tsql(table_name)}",
        limit=None,
    )
    return result["columns"], result["rows"], table_name or "dataset"


@router.get("/export_data")
def export_data(
    export_format: str = Query("csv", alias="format"),
    session_id: Optional[int] = None,
    query_id: int = -1,
    table_id: Optional[int] = None,
    columns: Optional[List[str]] = Query(default=None),
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    authed_user_id: int = Depends(get_current_user_id),
):
    if export_format not in _MIME:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'xlsx'.")
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="sort_dir must be 'asc' or 'desc'.")
    if session_id is None and table_id is None:
        raise HTTPException(status_code=400, detail="Provide either session_id or table_id.")

    try:
        if session_id is not None:
            headers, rows, base_name = _pipeline_data(session_id, query_id, authed_user_id)
        else:
            headers, rows, base_name = _table_data(table_id, authed_user_id)

        if sort_by:
            try:
                sort_index = headers.index(sort_by)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Unknown sort column: {sort_by}")
            rows = _sort_rows(rows, sort_index, sort_dir == "asc")

        headers, rows = _project(headers, rows, columns)
        filename = _filename(base_name, export_format)
        if export_format == "csv":
            return _csv_response(headers, rows, filename)
        return _xlsx_response(headers, rows, filename)

    except HTTPException:
        raise
    except Exception:
        logger.exception("Export failed (session=%s, table=%s)", session_id, table_id)
        raise HTTPException(
            status_code=500,
            detail={"code": "EXPORT_FAILED", "message": "The export couldn't be generated. Please try again.", "retryable": True},
        )
