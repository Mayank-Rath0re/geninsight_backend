# services/custom_db/introspection.py
"""
Schema discovery and metadata building for client-owned databases.

The LLM planner needs the same `metadata` / `knowledgebase` shape that uploaded
datasets get, so external tables are described mechanically from
INFORMATION_SCHEMA plus a small row sample (no AI call, no token cost).
"""

import json
import re

import pandas as pd

from core import config
from services import ingestion
from services.custom_db import dialects


def default_schema(conn, db_type: str):
    sql = {
        "postgres": "SELECT current_schema()",
        "mysql": "SELECT DATABASE()",
        "mssql": "SELECT SCHEMA_NAME()",
    }[db_type]
    cur = conn.cursor()
    try:
        cur.execute(sql)
        row = cur.fetchone()
    finally:
        cur.close()
    return row[0] if row else None


def list_tables(conn, db_type: str) -> list[str]:
    if db_type == "mysql":
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
    elif db_type == "postgres":
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
    else:
        sql = (
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA = SCHEMA_NAME() "
            "ORDER BY TABLE_NAME"
        )

    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()
    return [str(r[0]) for r in rows]


def list_columns(conn, db_type: str, table: str) -> list[tuple[str, str]]:
    cur = conn.cursor()
    try:
        if db_type == "mysql":
            cur.execute(f"SHOW COLUMNS FROM {dialects.quote_identifier(table, db_type)}")
            rows = cur.fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

        if db_type == "postgres":
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
        else:
            cur.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = %s AND TABLE_SCHEMA = SCHEMA_NAME() "
                "ORDER BY ORDINAL_POSITION",
                (table,),
            )
        rows = cur.fetchall()
    finally:
        cur.close()
    return [(str(r[0]), str(r[1])) for r in rows]


def sample_rows(conn, db_type: str, table: str, limit: int) -> dict:
    quoted = dialects.quote_identifier(table, db_type)
    if db_type == "mssql":
        sql = f"SELECT TOP {int(limit)} * FROM {quoted}"
    else:
        sql = f"SELECT * FROM {quoted} LIMIT {int(limit)}"

    cur = conn.cursor()
    try:
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        cur.close()
    return {"columns": columns, "rows": [list(r) for r in rows] if rows else []}


_NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "tinyint", "mediumint", "decimal", "numeric",
    "float", "double", "double precision", "real", "money", "smallmoney", "serial",
    "bigserial", "number", "dec", "fixed", "int2", "int4", "int8", "float4", "float8",
}
_DATETIME_TYPES = {
    "date", "datetime", "datetime2", "smalldatetime", "timestamp", "timestamptz",
    "timestamp with time zone", "timestamp without time zone", "time", "timetz",
    "time with time zone", "time without time zone", "datetimeoffset", "year",
}
_BOOLEAN_TYPES = {"bit", "bool", "boolean"}


def _base_type(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", (data_type or "")).strip().lower()


def _coerce_frame(columns: list[tuple[str, str]], sample: dict) -> pd.DataFrame:
    type_map = {name: data_type for name, data_type in columns}
    ordered = [c for c in sample["columns"] if c in type_map] or [c for c, _ in columns]
    data = {}
    for idx, name in enumerate(ordered):
        values = [row[idx] if idx < len(row) else None for row in sample["rows"]]
        base = _base_type(type_map.get(name, ""))
        if base in _NUMERIC_TYPES:
            data[name] = pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce")
        elif base in _DATETIME_TYPES:
            data[name] = pd.to_datetime(pd.Series(values), errors="coerce")
        elif base in _BOOLEAN_TYPES:
            data[name] = pd.Series(values, dtype=object)
        else:
            data[name] = pd.Series(
                [None if v is None else str(v) for v in values], dtype=object
            )
    return pd.DataFrame(data, columns=ordered)


def _empty_metadata(columns: list[tuple[str, str]]) -> str:
    meta = {}
    for name, data_type in columns:
        base = _base_type(data_type)
        dtype = (
            "float64" if base in _NUMERIC_TYPES
            else "datetime64[ns]" if base in _DATETIME_TYPES
            else "object"
        )
        meta[name] = {
            "dtype": dtype,
            "null_count": 0,
            "null_pct": 0.0,
            "unique_count": 0,
            "sample_values": [],
            "all_possible_values": [],
        }
    return json.dumps(meta)


def _knowledge_base(table: str, db_type: str, columns: list[tuple[str, str]], metadata: str, roles: str) -> str:
    role_map = json.loads(roles)
    metric = [c for c, r in role_map.items() if r.get("role") == "metric"]
    dates = [c for c, r in role_map.items() if r.get("role") == "date"]
    identifiers = [c for c, r in role_map.items() if r.get("role") == "identifier"]
    categorical = [c for c, r in role_map.items() if r.get("role") in ("dimension", "text")]

    return json.dumps({
        "columns_name": [name for name, _ in columns],
        "table_name": table,
        "domain": "external",
        "description": (
            f"Table '{table}' from a client-owned {db_type} database. "
            "Statistics are derived from a row sample, not the full table."
        ),
        "row_count": None,
        "column_count": len(columns),
        "key_columns": identifiers,
        "date_columns": dates,
        "numeric_columns": metric,
        "categorical_columns": categorical,
        "data_quality_notes": "Sample-based statistics; min/max/null figures cover the sample only.",
        "potential_analyses": [],
        "column_roles": role_map,
    })


def describe_table(conn, db_type: str, table: str) -> dict:
    """Return {name, columns, metadata, knowledgebase} for one remote table."""
    columns = list_columns(conn, db_type, table)
    if not columns:
        raise ValueError(f"Table '{table}' has no readable columns.")

    sample = sample_rows(conn, db_type, table, config.CUSTOM_DB_SAMPLE_ROWS)
    frame = _coerce_frame(columns, sample)

    if len(frame):
        metadata = ingestion.metadata_from_dataframe(frame)
    else:
        metadata = _empty_metadata(columns)
    roles = ingestion.classify_column_roles(frame, metadata)

    return {
        "name": table,
        "columns": [name for name, _ in columns],
        "metadata": metadata,
        "knowledgebase": _knowledge_base(table, db_type, columns, metadata, roles),
    }
