# core/db.py
"""
Database access layer (was db_handler.py).

Every function opens its own connection and closes it in a `finally` block
rather than relying on pymssql.Connection's `with conn:` protocol — that
protocol does not reliably close the underlying socket across
pymssql/FreeTDS versions, and behind a tunnel/proxy, connections are held
open long enough that leaked ones exhaust the SQL Server connection pool
and cause intermittent timeouts on unrelated endpoints.
"""

import csv
import logging
from contextlib import contextmanager
from typing import Any, List, Optional

import pymssql

from core.config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_DATABASE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection() -> pymssql.Connection:
    return pymssql.connect(
        server=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_DATABASE,
        tds_version="7.4",
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Transaction context manager
# ---------------------------------------------------------------------------

@contextmanager
def transaction():
    """
    Yield a cursor bound to a single connection/transaction.

    Usage:
        with db.transaction() as cur:
            cur.execute(...)
            cur.execute(...)
        # commits on success, rolls back on any exception
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def check_login_credentials(email: str, password: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT password FROM auth WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    return row is not None and password == row[0]


def get_single_value(query: str, params: tuple = None) -> Any:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        cur.close()
        conn.commit()
    finally:
        conn.close()
    return row[0] if row else None


def fetch(
    query: str,
    params: tuple = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[list]:
    """Always returns list[list] — no shape-dependent scalar collapse."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer.")
    if offset is not None and offset < 0:
        raise ValueError("offset must be a non-negative integer.")

    q = query.rstrip(";")
    if limit is not None:
        q += f" OFFSET {offset or 0} ROWS FETCH NEXT {limit} ROWS ONLY"
    q += ";"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(q, params or ())
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return [list(row) for row in rows] if rows else []


def fetch_with_columns(
    query: str,
    params: tuple = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict:
    """Like fetch(), but also returns column names from cur.description."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer.")
    if offset is not None and offset < 0:
        raise ValueError("offset must be a non-negative integer.")

    q = query.rstrip(";")
    if limit is not None:
        q += f" OFFSET {offset or 0} ROWS FETCH NEXT {limit} ROWS ONLY"
    q += ";"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(q, params or ())
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return {
        "columns": columns,
        "rows": [list(row) for row in rows] if rows else [],
    }


def insert(query: str, params: tuple = None) -> int:
    """Expects the query to contain `OUTPUT INSERTED.<col>` — no @@IDENTITY needed."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return row[0] if row else -1


def run(query: str, params: tuple = None) -> bool:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(query, params or ())
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return True


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _python_type_to_sql_type(value) -> str:
    if isinstance(value, bool):
        return "BIT"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, str):
        return "NVARCHAR(255)" if len(value) <= 255 else "NVARCHAR(MAX)"
    return "NVARCHAR(MAX)"


def generate_sql_schema(table_name: str, file_path: str) -> str:
    with open(file_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return (
            f"IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U') "
            f"CREATE TABLE [{table_name}] ([id] INT IDENTITY(1,1) PRIMARY KEY);"
        )

    columns = list(rows[0].keys())
    col_defs: List[str] = []

    pk_col = ""
    for col in columns:
        if col.lower() in ("id",) or col.lower().endswith("_id"):
            values = [r[col] for r in rows if r[col] != ""]
            if len(values) == len(rows) and len(set(values)) == len(rows):
                pk_col = col
                break

    if not pk_col:
        col_defs.append("[id] INT IDENTITY(1,1) PRIMARY KEY")

    for col in columns:
        samples = [r[col] for r in rows if r[col] != ""]
        sample = samples[0] if samples else ""
        try:
            int(sample)
            sql_type = "BIGINT"
        except ValueError:
            try:
                float(sample)
                sql_type = "FLOAT"
            except ValueError:
                sql_type = _python_type_to_sql_type(sample)

        col_defs.append(
            f"[{col}] {sql_type} PRIMARY KEY" if col == pk_col else f"[{col}] {sql_type}"
        )

    defs_str = ",\n  ".join(col_defs)
    return (
        f"IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U')\n"
        f"CREATE TABLE [{table_name}] (\n  {defs_str}\n);"
    )
