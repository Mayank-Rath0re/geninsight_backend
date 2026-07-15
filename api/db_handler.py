# db_handler.py

import logging
import os
import sys
import csv
from contextlib import contextmanager
from typing import Any, List, Optional

import pymssql
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

try:
    DB_HOST     = os.getenv("DB_HOST")
    DB_USER     = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_DATABASE = os.getenv("DB_DATABASE")

    db_port_env = os.getenv("DB_PORT")
    if not db_port_env:
        raise ValueError("DB_PORT environment variable is missing.")
    DB_PORT = int(db_port_env)

except (ValueError, TypeError) as e:
    logger.critical(f"Database credentials error: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_db_connection() -> pymssql.Connection:
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
# Transaction context manager  ← new addition
# ---------------------------------------------------------------------------

@contextmanager
def transaction():
    """
    Yield a cursor bound to a single connection/transaction.

    Usage:
        with db_handler.transaction() as cur:
            cur.execute(...)
            cur.execute(...)
        # commits on success, rolls back on any exception
    """
    conn = get_db_connection()
    cur  = conn.cursor()
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
# Existing functions — names unchanged, internals fixed
# ---------------------------------------------------------------------------

def check_login_credentials(email: str, password: str) -> bool:
    # Fixed: connection was never closed in the original
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT password FROM auth WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close()
    return row is not None and password == row[0]


def get_single_value_db(query: str, params: tuple = None) -> Any:
    # Unchanged behaviour, just uses context manager to guarantee connection closes
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        cur.close()
        conn.commit()
    return row[0] if row else None


def run_fetch_query(
    query: str,
    params: tuple = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    # Fixed: return type is now always list[list] — no more shape-dependent scalar collapse
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer.")
    if offset is not None and offset < 0:
        raise ValueError("offset must be a non-negative integer.")

    q = query.rstrip(";")
    if limit is not None:
        q += f" OFFSET {offset or 0} ROWS FETCH NEXT {limit} ROWS ONLY"
    q += ";"

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, params or ())
        rows = cur.fetchall()
        cur.close()

    return [list(row) for row in rows] if rows else []

def run_fetch_query_with_columns(
    query: str,
    params: tuple = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
):
    """Like run_fetch_query, but also returns column names from cur.description."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer.")
    if offset is not None and offset < 0:
        raise ValueError("offset must be a non-negative integer.")

    q = query.rstrip(";")
    if limit is not None:
        q += f" OFFSET {offset or 0} ROWS FETCH NEXT {limit} ROWS ONLY"
    q += ";"

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, params or ())
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()
        cur.close()

    return {
        "columns": columns,
        "rows": [list(row) for row in rows] if rows else [],
    }


def run_insert_query(query: str, params: tuple = None) -> int:
    # Fixed: removed @@IDENTITY — OUTPUT INSERTED.id in the query itself is reliable
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, params or ())
        row = cur.fetchone()
        conn.commit()
        cur.close()
    return row[0] if row else -1


def run_query(query: str, params: tuple = None) -> bool:
    # Fixed: cursor was never closed in the original
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, params or ())
        conn.commit()
        cur.close()
    return True


# ---------------------------------------------------------------------------
# Schema helpers — unchanged
# ---------------------------------------------------------------------------

def _python_type_to_sql_type(value) -> str:
    if isinstance(value, bool):  return "BIT"
    if isinstance(value, int):   return "BIGINT"
    if isinstance(value, float): return "FLOAT"
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

    columns  = list(rows[0].keys())
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
        sample  = samples[0] if samples else ""
        try:
            int(sample);   sql_type = "BIGINT"
        except ValueError:
            try:
                float(sample); sql_type = "FLOAT"
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