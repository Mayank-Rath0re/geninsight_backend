# services/custom_db/connectors.py
"""
Low-level connections and read-only execution against client-owned databases.

All three drivers speak the DB-API 2.0 interface with ``%s`` placeholders, so a
single execute/fetch path works for MySQL, Postgres and SQL Server. Driver imports
are lazy: a missing optional driver only breaks that one dialect.
"""

import logging
import time
from contextlib import contextmanager

from core import config
from services.custom_db import dialects

logger = logging.getLogger(__name__)


class ExternalDBError(Exception):
    """Connection or execution failure against a client-owned database."""


def _params(source: dict) -> dict:
    db_type = dialects.normalize_db_type(source.get("engine") or source.get("dbtype"))
    host = source.get("host") or source.get("dburl") or ""
    port = source.get("port") or source.get("dbport") or dialects.default_port(db_type)
    password = source.get("password")
    if password is None:
        password = source.get("dbpassword") or ""
    return {
        "db_type": db_type,
        "host": host,
        "port": int(port),
        "user": source.get("username") or source.get("dbuser") or "",
        "password": password,
        "database": source.get("database_name") or source.get("dbname") or "",
        "ssl": bool(source.get("ssl_enabled") or source.get("ssl") or False),
    }


def connect(source: dict):
    p = _params(source)
    timeout = max(1, config.CUSTOM_DB_CONNECT_TIMEOUT)
    query_timeout = max(1, config.CUSTOM_DB_QUERY_TIMEOUT)

    try:
        if p["db_type"] == "mysql":
            import pymysql

            conn = pymysql.connect(
                host=p["host"],
                port=p["port"],
                user=p["user"],
                password=p["password"],
                database=p["database"] or None,
                charset="utf8mb4",
                connect_timeout=timeout,
                read_timeout=query_timeout,
                write_timeout=query_timeout,
                # Require TLS but don't demand a CA (MySQL's auto-generated certs
                # are self-signed); mirrors Postgres' sslmode=require.
                ssl={"check_hostname": False} if p["ssl"] else None,
                autocommit=True,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {query_timeout * 1000}")
            except Exception:
                pass  # not supported on MariaDB/older MySQL — driver timeouts still apply
            return conn

        if p["db_type"] == "postgres":
            import psycopg2

            conn = psycopg2.connect(
                host=p["host"],
                port=p["port"],
                user=p["user"],
                password=p["password"],
                dbname=p["database"],
                connect_timeout=timeout,
                sslmode="require" if p["ssl"] else "prefer",
                application_name="geninsight",
            )
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {query_timeout * 1000}")
                cur.execute("SET default_transaction_read_only = on")
            return conn

        # SQL Server
        import pymssql

        conn = pymssql.connect(
            server=p["host"],
            port=p["port"],
            user=p["user"],
            password=p["password"],
            database=p["database"] or None,
            login_timeout=timeout,
            timeout=query_timeout,
            tds_version="7.4",
            autocommit=True,
        )
        cur = conn.cursor()
        try:
            cur.execute(f"SET LOCK_TIMEOUT {query_timeout * 1000}")
        finally:
            cur.close()
        return conn

    except ExternalDBError:
        raise
    except Exception as exc:
        raise ExternalDBError(_friendly(exc)) from exc


def _friendly(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


@contextmanager
def connection(source: dict):
    conn = connect(source)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _cell(value):
    """JSON-safe cell: BLOB/BINARY values would otherwise fail response encoding."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    return value


def query_on(conn, sql: str, params=None) -> dict:
    """Execute on an already-open connection. Returns {columns, rows}."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()
    except Exception as exc:
        raise ExternalDBError(_friendly(exc)) from exc
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return {"columns": columns, "rows": [[_cell(v) for v in row] for row in rows] if rows else []}


def fetch(source: dict, sql: str, params=None, limit=None, offset: int = 0) -> dict:
    """Open a connection, run `sql` with dialect pagination, close it."""
    db_type = dialects.normalize_db_type(source.get("engine") or source.get("dbtype"))
    paginated = dialects.apply_row_limit(sql, db_type, limit=limit, offset=offset)
    with connection(source) as conn:
        return query_on(conn, paginated, params=params)


def test_connection(source: dict) -> dict:
    """Verify credentials and return the server version plus round-trip latency."""
    db_type = dialects.normalize_db_type(source.get("engine") or source.get("dbtype"))
    version_sql = {
        "postgres": "SELECT version()",
        "mysql": "SELECT VERSION()",
        "mssql": "SELECT @@VERSION",
    }[db_type]

    started = time.perf_counter()
    with connection(source) as conn:
        version = query_on(conn, version_sql)["rows"][0][0]
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    return {"server_version": str(version).splitlines()[0][:200], "latency_ms": latency_ms}
