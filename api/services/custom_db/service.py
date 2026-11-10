# services/custom_db/service.py
"""
CRUD, connection testing, schema introspection and ad-hoc execution for
client-owned databases (rows in `data_sources`).

Passwords are encrypted at rest (core.crypto). Tables exposed to the analyst are
registered in `table_info` with `data_source_id` set; no data is copied into
genbi_db and physical tables are never created or dropped for these sources.
"""

import logging

from fastapi import HTTPException

from core import config, crypto, db
from services import billing_service, dataset_cleanup
from services.custom_db import connectors, dialects, introspection

logger = logging.getLogger("my_global_app_logger")

MAX_NAME_LENGTH = 150
MAX_HOST_LENGTH = 255
MAX_USER_LENGTH = 255

_SOURCE_COLS = (
    "id, user_id, name, engine, host, port, database_name, username, "
    "secret_encrypted, ssl_enabled, status, last_connected_at, last_error, created_at"
)


def _iso(value):
    return value.isoformat() if value else None


def _public(row, table_count: int = 0) -> dict:
    return {
        "id": row[0],
        "userId": row[1],
        "name": row[2],
        "dbtype": row[3],
        "dburl": row[4],
        "dbport": row[5],
        "dbname": row[6],
        "dbuser": row[7],
        "has_password": bool(row[8]),
        "ssl": bool(row[9]),
        "status": row[10],
        "last_connected_at": _iso(row[11]),
        "last_error": row[12],
        "created_at": _iso(row[13]),
        "table_count": table_count,
    }


def _connect_params(row) -> dict:
    return {
        "id": row[0],
        "engine": row[3],
        "host": row[4],
        "port": row[5],
        "database_name": row[6],
        "username": row[7],
        "password": crypto.decrypt_secret(row[8]),
        "ssl_enabled": bool(row[9]),
        "status": row[10],
    }


def _fetch_source(source_id: int):
    rows = db.fetch(
        f"SELECT {_SOURCE_COLS} FROM data_sources WHERE id = %s AND is_deleted = 0",
        params=(source_id,),
    )
    return rows[0] if rows else None


def _get_owned_source(user_id: int, source_id: int):
    row = _fetch_source(source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Database connection not found.")
    if row[1] != user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")
    return row


def _table_counts(user_id: int) -> dict:
    rows = db.fetch(
        "SELECT data_source_id, COUNT(*) FROM table_info "
        "WHERE user_id = %s AND data_source_id IS NOT NULL AND is_deleted = 0 "
        "GROUP BY data_source_id",
        params=(user_id,),
    )
    return {r[0]: int(r[1]) for r in rows}


def _usage(user_id: int) -> tuple[int, int | None]:
    used = int(db.get_single_value(
        "SELECT COUNT(*) FROM data_sources WHERE user_id = %s AND is_deleted = 0", (user_id,)
    ) or 0)
    plan = billing_service.get_entitlement(user_id).plan
    return used, plan.max_data_sources


def _assert_can_create(user_id: int) -> None:
    used, limit = _usage(user_id)
    if limit is not None and used >= limit:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "DATA_SOURCE_LIMIT",
                "message": f"Your plan allows {limit} database connection(s).",
                "limit": limit,
                "used": used,
            },
        )


def _validate_details(payload: dict, require_password: bool) -> dict:
    try:
        dbtype = dialects.normalize_db_type(payload.get("dbtype"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    host = dialects.extract_host(payload.get("dburl"))
    if not host:
        raise HTTPException(status_code=400, detail="dburl is required.")
    if len(host) > MAX_HOST_LENGTH:
        raise HTTPException(status_code=400, detail="dburl is too long.")

    port = payload.get("dbport") or dialects.default_port(dbtype)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="dbport must be an integer.")
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="dbport must be between 1 and 65535.")

    dbname = (payload.get("dbname") or "").strip()
    if not dbname:
        raise HTTPException(status_code=400, detail="dbname is required.")
    if len(dbname) > 255:
        raise HTTPException(status_code=400, detail="dbname is too long.")

    dbuser = (payload.get("dbuser") or "").strip()
    if not dbuser:
        raise HTTPException(status_code=400, detail="dbuser is required.")
    if len(dbuser) > MAX_USER_LENGTH:
        raise HTTPException(status_code=400, detail="dbuser is too long.")

    password = payload.get("dbpassword")
    if require_password and password is None:
        raise HTTPException(status_code=400, detail="dbpassword is required.")
    if password is not None and len(password) > 1024:
        raise HTTPException(status_code=400, detail="dbpassword is too long.")

    name = (payload.get("name") or dbname).strip()[:MAX_NAME_LENGTH] or dbname

    return {
        "dbtype": dbtype,
        "host": host,
        "port": port,
        "dbname": dbname,
        "dbuser": dbuser,
        "dbpassword": password,
        "name": name,
        "ssl": bool(payload.get("ssl")),
    }


def _connection_source(details: dict, password: str | None = None) -> dict:
    return {
        "engine": details["dbtype"],
        "host": details["host"],
        "port": details["port"],
        "database_name": details["dbname"],
        "username": details["dbuser"],
        "password": password if password is not None else details.get("dbpassword"),
        "ssl_enabled": details["ssl"],
    }


def _introspect(source: dict, selected_tables=None) -> tuple[list[dict], str | None]:
    dbtype = dialects.normalize_db_type(source["engine"])
    with connectors.connection(source) as conn:
        schema = introspection.default_schema(conn, dbtype)
        available = introspection.list_tables(conn, dbtype)

        if selected_tables is None:
            tables = available
        else:
            available_set = set(available)
            tables = [t for t in selected_tables if t]
            missing = [t for t in tables if t not in available_set]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown table(s) in this database: {', '.join(missing[:10])}",
                )

        if len(tables) > config.CUSTOM_DB_MAX_TABLES_PER_SOURCE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Select at most {config.CUSTOM_DB_MAX_TABLES_PER_SOURCE} tables per "
                    "database connection."
                ),
            )

        described = [introspection.describe_table(conn, dbtype, t) for t in tables]
    return described, schema


def _register_tables(cur, user_id: int, source_id: int, schema, described: list[dict]) -> list[dict]:
    registered = []
    for table in described:
        cur.execute(
            "SELECT id FROM table_info WHERE user_id = %s AND data_source_id = %s "
            "AND name = %s AND is_deleted = 0",
            (user_id, source_id, table["name"]),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE table_info SET knowledgebase = %s, metadata = %s, source_schema = %s "
                "WHERE id = %s",
                (table["knowledgebase"], table["metadata"], schema, existing[0]),
            )
            table_id = existing[0]
        else:
            cur.execute(
                "INSERT INTO table_info "
                "(name, user_id, data_source_id, source_table, source_schema, knowledgebase, metadata, createdAt) "
                "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s, %s, %s, SYSDATETIME())",
                (
                    table["name"], user_id, source_id, table["name"], schema,
                    table["knowledgebase"], table["metadata"],
                ),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Failed to register table {table['name']}.")
            table_id = row[0]
        registered.append({"id": table_id, "name": table["name"]})
    return registered


def list_sources(user_id: int) -> dict:
    rows = db.fetch(
        f"SELECT {_SOURCE_COLS} FROM data_sources "
        "WHERE user_id = %s AND is_deleted = 0 ORDER BY created_at DESC, id DESC",
        params=(user_id,),
    )
    counts = _table_counts(user_id)
    used, limit = _usage(user_id)
    return {
        "data_sources": [_public(r, counts.get(r[0], 0)) for r in rows],
        "limit": limit,
        "used": used,
    }


def get_source(user_id: int, source_id: int) -> dict:
    row = _get_owned_source(user_id, source_id)
    return _public(row, _table_counts(user_id).get(source_id, 0))


def test_connection(payload: dict) -> dict:
    details = _validate_details(payload, require_password=True)
    try:
        dialects.assert_host_allowed(details["host"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return connectors.test_connection(_connection_source(details))
    except connectors.ExternalDBError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "DATA_SOURCE_CONNECTION_FAILED", "message": str(exc)},
        )


def preview_tables(payload: dict) -> dict:
    """List base tables for credentials that are not saved yet (Add-DB wizard)."""
    details = _validate_details(payload, require_password=True)
    try:
        dialects.assert_host_allowed(details["host"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    source = _connection_source(details)
    try:
        with connectors.connection(source) as conn:
            dbtype = dialects.normalize_db_type(source["engine"])
            schema = introspection.default_schema(conn, dbtype)
            available = introspection.list_tables(conn, dbtype)
    except connectors.ExternalDBError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "DATA_SOURCE_CONNECTION_FAILED", "message": str(exc)},
        )

    return {
        "schema": schema,
        "tables": available,
        "max_selectable": config.CUSTOM_DB_MAX_TABLES_PER_SOURCE,
    }


def create_source(user_id: int, payload: dict, selected_tables=None) -> dict:
    details = _validate_details(payload, require_password=True)
    _assert_can_create(user_id)

    try:
        dialects.assert_host_allowed(details["host"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    source = _connection_source(details)
    try:
        connection_info = connectors.test_connection(source)
    except connectors.ExternalDBError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "DATA_SOURCE_CONNECTION_FAILED", "message": str(exc)},
        )

    try:
        described, schema = _introspect(source, selected_tables)
    except connectors.ExternalDBError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "DATA_SOURCE_INTROSPECTION_FAILED", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    encrypted = crypto.encrypt_secret(details["dbpassword"] or "")

    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO data_sources "
            "(user_id, name, engine, host, port, database_name, username, secret_encrypted, "
            " ssl_enabled, status, last_connected_at) "
            "OUTPUT INSERTED.id VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', SYSDATETIME())",
            (
                user_id, details["name"], details["dbtype"], details["host"], details["port"],
                details["dbname"], details["dbuser"], encrypted, 1 if details["ssl"] else 0,
            ),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="Failed to store the database connection.")
        source_id = row[0]
        registered = _register_tables(cur, user_id, source_id, schema, described)

    logger.info(
        "Custom DB %s added by user %s (%s %s:%s/%s, %s tables)",
        source_id, user_id, details["dbtype"], details["host"], details["port"],
        details["dbname"], len(registered),
    )
    return {
        "data_source": get_source(user_id, source_id),
        "tables": registered,
        "connection": connection_info,
    }


def update_source(user_id: int, source_id: int, payload: dict, selected_tables=None) -> dict:
    row = _get_owned_source(user_id, source_id)

    current = {
        "dbtype": row[3],
        "dburl": row[4],
        "dbport": row[5],
        "dbname": row[6],
        "dbuser": row[7],
        "name": row[2],
        "ssl": bool(row[9]),
    }
    merged = dict(current)
    for key in ("dbtype", "dburl", "dbport", "dbname", "dbuser", "name", "ssl"):
        if key in payload and payload[key] is not None:
            merged[key] = payload[key]
    merged["dbpassword"] = payload.get("dbpassword")

    details = _validate_details(merged, require_password=False)

    try:
        dialects.assert_host_allowed(details["host"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    password = details["dbpassword"]
    if password is None:
        password = crypto.decrypt_secret(row[8])
    source = _connection_source(details, password=password)

    try:
        connection_info = connectors.test_connection(source)
    except connectors.ExternalDBError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "DATA_SOURCE_CONNECTION_FAILED", "message": str(exc)},
        )

    described = schema = None
    if selected_tables is None:
        # Pointing the connection somewhere else invalidates the stored metadata,
        # so re-describe the tables that are already registered against this source.
        connection_changed = (
            details["dbtype"] != row[3]
            or details["host"] != row[4]
            or int(details["port"]) != int(row[5])
            or details["dbname"] != row[6]
            or details["dbuser"] != row[7]
        )
        if connection_changed:
            selected_tables = [
                r[0] for r in db.fetch(
                    "SELECT name FROM table_info WHERE data_source_id = %s "
                    "AND is_deleted = 0 ORDER BY name",
                    params=(source_id,),
                )
            ] or None

    if selected_tables is not None:
        try:
            described, schema = _introspect(source, selected_tables)
        except connectors.ExternalDBError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "DATA_SOURCE_INTROSPECTION_FAILED", "message": str(exc)},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    encrypted = crypto.encrypt_secret(password or "")

    with db.transaction() as cur:
        cur.execute(
            "UPDATE data_sources SET name = %s, engine = %s, host = %s, port = %s, "
            "database_name = %s, username = %s, secret_encrypted = %s, ssl_enabled = %s, "
            "status = 'active', last_connected_at = SYSDATETIME(), last_error = NULL, "
            "updated_at = SYSDATETIME() WHERE id = %s",
            (
                details["name"], details["dbtype"], details["host"], details["port"],
                details["dbname"], details["dbuser"], encrypted, 1 if details["ssl"] else 0,
                source_id,
            ),
        )

        registered = []
        removed = []
        if described is not None:
            registered = _register_tables(cur, user_id, source_id, schema, described)

            keep = {t["name"] for t in described}
            cur.execute(
                "SELECT id, name FROM table_info WHERE data_source_id = %s AND is_deleted = 0",
                (source_id,),
            )
            for table_id, table_name in cur.fetchall():
                if table_name not in keep:
                    dataset_cleanup.soft_delete_dataset(cur, table_id)
                    removed.append({"id": table_id, "name": table_name})

    logger.info(
        "Custom DB %s updated by user %s (%s tables registered, %s unregistered)",
        source_id, user_id, len(registered), len(removed),
    )
    result = {
        "data_source": get_source(user_id, source_id),
        "connection": connection_info,
    }
    if described is not None:
        result["tables"] = registered
        result["removed_tables"] = removed
    return result


def delete_source(user_id: int, source_id: int) -> dict:
    _get_owned_source(user_id, source_id)

    with db.transaction() as cur:
        cur.execute(
            "SELECT id FROM table_info WHERE data_source_id = %s AND is_deleted = 0",
            (source_id,),
        )
        table_ids = [r[0] for r in cur.fetchall()]
        for table_id in table_ids:
            dataset_cleanup.soft_delete_dataset(cur, table_id)

        cur.execute(
            "UPDATE data_sources SET is_deleted = 1, deleted_at = SYSDATETIME(), "
            "status = 'revoked', secret_encrypted = '', updated_at = SYSDATETIME() "
            "WHERE id = %s",
            (source_id,),
        )

    logger.info("Custom DB %s removed by user %s (%s tables unregistered)",
                source_id, user_id, len(table_ids))
    return {"id": source_id, "deleted": True, "removed_tables": len(table_ids)}


def remote_tables(user_id: int, source_id: int) -> dict:
    row = _get_owned_source(user_id, source_id)
    source = _connect_params(row)

    try:
        with connectors.connection(source) as conn:
            dbtype = dialects.normalize_db_type(source["engine"])
            schema = introspection.default_schema(conn, dbtype)
            available = introspection.list_tables(conn, dbtype)
    except connectors.ExternalDBError as exc:
        _mark_status(source_id, False, str(exc))
        raise HTTPException(
            status_code=502,
            detail={"code": "DATA_SOURCE_UNAVAILABLE", "message": str(exc)},
        )

    registered = {
        r[0] for r in db.fetch(
            "SELECT name FROM table_info WHERE data_source_id = %s AND is_deleted = 0",
            params=(source_id,),
        )
    }
    return {
        "data_source_id": source_id,
        "schema": schema,
        "tables": [
            {"name": name, "registered": name in registered} for name in available
        ],
    }


def _mark_status(source_id: int, ok: bool, error: str | None = None) -> None:
    try:
        db.run(
            "UPDATE data_sources SET status = %s, last_connected_at = SYSDATETIME(), "
            "last_error = %s, updated_at = SYSDATETIME() WHERE id = %s",
            ("active" if ok else "unreachable", None if ok else (error or "")[:4000], source_id),
        )
    except Exception:
        logger.exception("Failed to update status for data source %s", source_id)


def run_query(user_id: int, source_id: int, sql: str, limit: int | None = None) -> dict:
    """Run the caller's SQL against the target database in ITS native dialect."""
    from core.sql_safety import is_safe_select

    row = _get_owned_source(user_id, source_id)
    if row[10] == "revoked":
        raise HTTPException(status_code=409, detail="This database connection was revoked.")

    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="sql is required.")
    ok, why = is_safe_select(sql)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Only read-only SELECT queries are allowed: {why}.")

    max_rows = max(1, config.CUSTOM_DB_MAX_ROWS)
    requested = max_rows if limit is None else max(1, min(int(limit), max_rows))

    source = _connect_params(row)
    try:
        result = connectors.fetch(source, sql, limit=requested, offset=0)
    except connectors.ExternalDBError as exc:
        _mark_status(source_id, False, str(exc))
        raise HTTPException(
            status_code=502,
            detail={"code": "DATA_SOURCE_QUERY_FAILED", "message": str(exc)},
        )

    _mark_status(source_id, True)
    rows = result["rows"][:requested]
    return {
        "data_source_id": source_id,
        "columns": result["columns"],
        "rows": rows,
        "row_count": len(rows),
        "limit": requested,
    }
