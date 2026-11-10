# services/custom_db/dialects.py
"""
Dialect helpers for client-owned databases.

Everything the app generates and stores is T-SQL (the internal database's dialect).
When a query targets a custom MySQL/Postgres database, the assembled T-SQL is
transpiled once, here, right before execution. This keeps the generator, pipeline
assembly, repair loop and stored history dialect-independent.
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import sqlglot

from core import config
from core.sql_safety import mask_sql

SUPPORTED_DB_TYPES = ("mysql", "mssql", "postgres")

_DB_TYPE_ALIASES = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "sql_server": "mssql",
    "sql server": "mssql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "psql": "postgres",
    "pgsql": "postgres",
}

DEFAULT_PORTS = {"mysql": 3306, "mssql": 1433, "postgres": 5432}

# sqlglot writer names; mssql is executed as-is (no write dialect needed).
_WRITE_DIALECT = {"mysql": "mysql", "postgres": "postgres"}

# T-SQL only allows ORDER BY in a CTE when TOP/OFFSET is present, so the pipeline
# appends `OFFSET 0 ROWS`. MySQL/Postgres reject OFFSET without LIMIT (and 0 rows
# is a no-op anyway) — drop it, but never when a FETCH follows (that is a real
# top-N: `OFFSET 0 ROWS FETCH NEXT n ROWS ONLY`).
_NOOP_OFFSET_ZERO = re.compile(r"\bOFFSET\s+0\s+ROWS?(?!\s+FETCH\b)", re.I)
_TRAILING_OFFSET_ZERO = re.compile(r"\bOFFSET\s+0\s+ROWS?\s*$", re.I)


def normalize_db_type(value: str) -> str:
    key = (value or "").strip().lower().replace("-", "_")
    db_type = _DB_TYPE_ALIASES.get(key)
    if not db_type:
        raise ValueError(
            f"Unsupported dbtype '{value}'. Supported: SQL Server, MySQL, Postgres."
        )
    return db_type


def default_port(db_type: str) -> int:
    return DEFAULT_PORTS[db_type]


def extract_host(db_url: str) -> str:
    """Accept either a bare hostname/IP or a URL such as mysql://host:3306/db."""
    value = (db_url or "").strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.hostname:
            return parsed.hostname
    return value


def quote_identifier(name: str, db_type: str) -> str:
    """Quote a single identifier for the target dialect (escaping the quote char)."""
    if db_type == "mysql":
        return "`" + str(name).replace("`", "``") + "`"
    if db_type == "postgres":
        return '"' + str(name).replace('"', '""') + '"'
    return "[" + str(name).replace("]", "]]") + "]"


def to_native_sql(sql: str, db_type: str) -> str:
    """Translate generated T-SQL into the target dialect. SQL Server is a no-op."""
    db_type = normalize_db_type(db_type)
    if db_type == "mssql":
        return sql

    source_sql = sql
    if db_type in ("mysql", "postgres"):
        source_sql = _NOOP_OFFSET_ZERO.sub("", source_sql)

    try:
        translated = sqlglot.transpile(
            source_sql,
            read="tsql",
            write=_WRITE_DIALECT[db_type],
            pretty=False,
        )
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"Could not translate the query for {db_type}: {exc}") from exc

    if not translated:
        raise ValueError(f"Could not translate the query for {db_type}.")
    return translated[0]


def apply_row_limit(sql: str, db_type: str, limit=None, offset: int = 0) -> str:
    """Append dialect-correct pagination to a top-level SELECT/CTE.

    Queries whose T-SQL already carried TOP/OFFSET..FETCH keep that bound (it
    survived transpilation as LIMIT on MySQL/Postgres), so no second LIMIT is
    appended — `LIMIT 200 LIMIT 5000` is a syntax error.
    """
    if limit is None and not offset:
        return sql

    sql = _TRAILING_OFFSET_ZERO.sub("", sql.rstrip()).rstrip()
    top = mask_sql(sql, nested=True)

    if db_type in ("mysql", "postgres"):
        if re.search(r"\b(LIMIT|OFFSET)\b", top, re.I):
            return sql
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {int(limit)}")
        if offset:
            parts.append(f"OFFSET {int(offset)}")
        return sql + (" " + " ".join(parts) if parts else "")

    # SQL Server: OFFSET/FETCH requires ORDER BY on the outer query.
    if re.search(r"\b(TOP|FETCH)\b", top, re.I):
        return sql
    if limit is None:
        limit = 2**31 - 1
    if not re.search(r"\bORDER\s+BY\b", top, re.I):
        sql += " ORDER BY (SELECT NULL)"
    return sql + f" OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY"


_ALWAYS_BLOCKED = ("is_link_local", "is_multicast", "is_unspecified", "is_reserved")


def assert_host_allowed(host: str) -> None:
    """Reject hosts that could be used for SSRF against our own infrastructure."""
    if not host:
        raise ValueError("Database host is required.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve database host '{host}'.") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if any(getattr(ip, flag) for flag in _ALWAYS_BLOCKED):
            raise ValueError("Connections to link-local/reserved addresses are not allowed.")
        if ip.is_private and not config.ALLOW_PRIVATE_DB_HOSTS:
            raise ValueError(
                "Connections to loopback/private network addresses are not allowed. "
                "Set ALLOW_PRIVATE_DB_HOSTS=1 if this database is reachable over a private network."
            )
