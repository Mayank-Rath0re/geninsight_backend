# services/custom_db/execution.py
"""
Execution routing: decides whether a session/table lives in our own SQL Server or
in a client-owned database, and runs the query in the right place.

This is the single choke point the pipeline, dataset previews and dashboards use.
Queries are still generated and stored as T-SQL; `dialects.to_native_sql` converts
them for MySQL/Postgres only at execution time.
"""

import logging

from fastapi import HTTPException

from core import config, crypto, db
from core.sql_safety import is_safe_select
from services.custom_db import connectors, dialects, service as source_service

logger = logging.getLogger("my_global_app_logger")

_SOURCE_FIELDS = (
    "ti.data_source_id, ds.id, ds.engine, ds.host, ds.port, ds.database_name, "
    "ds.username, ds.secret_encrypted, ds.ssl_enabled, ds.status"
)


def _source_from_row(row, label: str) -> dict | None:
    data_source_id, source_id, engine, host, port, database_name, username, secret, ssl, status = row
    if data_source_id is None:
        return None
    if source_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"The database connection for this {label} no longer exists.",
        )
    if status == "revoked":
        raise HTTPException(
            status_code=409,
            detail=f"The database connection for this {label} was revoked.",
        )
    return {
        "id": source_id,
        "engine": engine,
        "host": host,
        "port": port,
        "database_name": database_name,
        "username": username,
        "password": crypto.decrypt_secret(secret),
        "ssl_enabled": bool(ssl),
        "status": status,
    }


def resolve_session_source(session_id: int) -> dict | None:
    rows = db.fetch(
        f"SELECT {_SOURCE_FIELDS} "
        "FROM sessions s JOIN table_info ti ON ti.id = s.table_id "
        "LEFT JOIN data_sources ds ON ds.id = ti.data_source_id AND ds.is_deleted = 0 "
        "WHERE s.session_id = %s",
        params=(session_id,),
    )
    if not rows:
        raise ValueError(f"No table found for session {session_id}.")
    return _source_from_row(rows[0], "session")


def resolve_table_source(table_id: int) -> dict | None:
    rows = db.fetch(
        f"SELECT {_SOURCE_FIELDS} "
        "FROM table_info ti "
        "LEFT JOIN data_sources ds ON ds.id = ti.data_source_id AND ds.is_deleted = 0 "
        "WHERE ti.id = %s",
        params=(table_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Table {table_id} not found.")
    return _source_from_row(rows[0], "table")


def session_data_source_id(session_id: int) -> int | None:
    """Cheap ownership/dialect check used to block cross-database merges."""
    rows = db.fetch(
        "SELECT ti.data_source_id FROM sessions s "
        "JOIN table_info ti ON ti.id = s.table_id WHERE s.session_id = %s",
        params=(session_id,),
    )
    return rows[0][0] if rows else None


def _internal_fetch(sql: str, limit, offset: int) -> dict:
    # Unchanged behaviour: our own SQL Server runs the query as-is (dashboard and
    # preview SQL already carry their own TOP bound), then rows are sliced here.
    result = db.fetch_with_columns(sql)
    rows = result["rows"]
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[:limit]
    return {"columns": result["columns"], "rows": rows}


def _external_fetch(source: dict, sql: str, limit, offset: int) -> dict:
    ok, why = is_safe_select(sql)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"Only read-only SELECT queries can run against a custom database: {why}.",
        )

    try:
        native_sql = dialects.to_native_sql(sql, source["engine"])
        result = connectors.fetch(source, native_sql, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except connectors.ExternalDBError as exc:
        source_service._mark_status(source["id"], False, str(exc))
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DATA_SOURCE_QUERY_FAILED",
                "message": f"The custom database query failed: {exc}",
                "data_source_id": source["id"],
            },
        )

    source_service._mark_status(source["id"], True)
    return result


def run_for_session(session_id: int, sql: str, limit=None, offset: int = 0) -> dict:
    source = resolve_session_source(session_id)
    if source is None:
        return _internal_fetch(sql, limit, offset)

    effective = config.CUSTOM_DB_MAX_ROWS if limit is None else min(limit, config.CUSTOM_DB_MAX_ROWS)
    return _external_fetch(source, sql, effective, offset)


def run_for_table(table_id: int, sql: str, limit=None, offset: int = 0) -> dict:
    source = resolve_table_source(table_id)
    if source is None:
        return _internal_fetch(sql, limit, offset)

    effective = config.CUSTOM_DB_MAX_ROWS if limit is None else min(limit, config.CUSTOM_DB_MAX_ROWS)
    return _external_fetch(source, sql, effective, offset)


def quote_tsql(name: str) -> str:
    return dialects.quote_identifier(name, "mssql")


def fetch_columns_for_table(table_id: int, table_name: str) -> list:
    sql = f"SELECT TOP 0 * FROM {quote_tsql(table_name)}"
    return run_for_table(table_id, sql, limit=None)["columns"]


def count_rows_for_table(table_id: int, table_name: str):
    """Exact row count; custom-DB failures degrade to None instead of failing a preview."""
    sql = f"SELECT COUNT(*) FROM {quote_tsql(table_name)}"
    try:
        result = run_for_table(table_id, sql, limit=None)
    except HTTPException:
        return None
    return int(result["rows"][0][0]) if result["rows"] else None
