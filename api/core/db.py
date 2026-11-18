# core/db.py
"""
Database access layer
"""

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

# SQL Server allows at most 2100 parameters per statement; stay well under it so
# one multi-row INSERT never tips over the limit.
INSERT_MAX_PARAMS = 2000


def _series_sql_type(series) -> str:
    """Map a pandas column directly to a SQL type, so the schema no longer needs
    a second pass over the raw file."""
    from pandas.api import types as ptypes

    if ptypes.is_bool_dtype(series):
        return "BIT"
    if ptypes.is_integer_dtype(series):
        return "BIGINT"
    if ptypes.is_float_dtype(series):
        return "FLOAT"
    if ptypes.is_datetime64_any_dtype(series):
        return "DATETIME2"

    non_null = series.dropna()
    if non_null.empty:
        return "NVARCHAR(255)"
    max_len = int(non_null.astype(str).str.len().max() or 0)
    return "NVARCHAR(MAX)" if max_len > 255 else "NVARCHAR(255)"


def generate_sql_schema(table_name: str, dataframe) -> str:
    columns = list(dataframe.columns)
    if dataframe.empty or not columns:
        return (
            f"IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U') "
            f"CREATE TABLE [{table_name}] ([id] INT IDENTITY(1,1) PRIMARY KEY);"
        )

    pk_col = ""
    for col in columns:
        if col.lower() == "id" or col.lower().endswith("_id"):
            series = dataframe[col]
            if series.notna().all() and series.is_unique:
                pk_col = col
                break

    col_defs: List[str] = []
    if not pk_col:
        col_defs.append("[id] INT IDENTITY(1,1) PRIMARY KEY")

    for col in columns:
        sql_type = _series_sql_type(dataframe[col])
        col_defs.append(
            f"[{col}] {sql_type} PRIMARY KEY" if col == pk_col else f"[{col}] {sql_type}"
        )

    defs_str = ",\n  ".join(col_defs)
    return (
        f"IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='{table_name}' AND xtype='U')\n"
        f"CREATE TABLE [{table_name}] (\n  {defs_str}\n);"
    )


def max_rows_per_insert(column_count: int) -> int:
    """Rows that fit in one multi-row VALUES clause for the given column count."""
    return max(1, min(1000, INSERT_MAX_PARAMS // max(1, column_count)))


def insert_rows(cursor, table_name: str, columns: List[str], rows: list) -> int:
    """Insert `rows` (list of tuples, already matching `columns`) as one
    multi-row INSERT. Callers batch with max_rows_per_insert(); a per-row
    INSERT is one network round trip each, which is what made large uploads
    take minutes."""
    if not rows:
        return 0

    col_list = ", ".join(f"[{c}]" for c in columns)
    row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
    values = ", ".join([row_placeholder] * len(rows))
    params = tuple(value for row in rows for value in row)
    cursor.execute(
        f"INSERT INTO [{table_name}] ({col_list}) VALUES {values}",
        params,
    )
    return len(rows)
