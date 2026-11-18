import json
import re

from core import db
from core.sql_safety import mask_sql
from services.custom_db import execution

# Rows returned inline when a step is created (transform/merge). The client
# pages the rest through /pipeline_result instead of holding the whole table.
FIRST_PAGE_ROWS = 200

def _fetch_session_table_name(session_id: int) -> str:
    rows = db.fetch(
        "SELECT t.name FROM table_info t "
        "JOIN sessions s ON s.table_id = t.id "
        "WHERE s.session_id = %s",
        params=(session_id,),
    )
    if not rows:
        raise ValueError(f"No table found for session {session_id}.")
    return rows[0][0]

def _replace_table_with_prev(step_sql: str, table_name: str, prev_alias: str) -> str:
    escaped = re.escape(table_name)
    pattern = rf'(?<![\w\]"])(?:\[{escaped}\]|"{escaped}"|{escaped})(?![\w\["])'
    return re.sub(pattern, prev_alias, step_sql, flags=re.IGNORECASE)

def _fetch_ordered_query_ids(session_id: int, query_id: int) -> list:
    rows = db.fetch(
        "SELECT id FROM queries WHERE session_id = %s AND is_deleted = 0 "
        "ORDER BY created_at ASC, id ASC",
        params=(session_id,),
    )
    if not rows:
        raise ValueError(f"Session {session_id} is empty or does not exist.")

    all_query_ids = [row[0] for row in rows]

    if query_id == -1:
        return all_query_ids

    try:
        cut = all_query_ids.index(query_id)
    except ValueError:
        raise ValueError(
            f"query_id={query_id} not found in session {session_id}. "
            f"Pipeline contains: {all_query_ids}"
        )
    return all_query_ids[: cut + 1]

def _fetch_step_rows(query_ids: list) -> dict:
    if not query_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(query_ids))
    rows = db.fetch(
        f"SELECT id, sql_query, step_type, source_session_id FROM queries WHERE id IN ({placeholders})",
        params=tuple(query_ids),
    )
    return {
        row[0]: {"sql_query": row[1], "step_type": row[2], "source_session_id": row[3]}
        for row in rows
    }

def _lift_nested_with(step_sql: str, prefix: str):
    if not re.match(r"\s*WITH\b", step_sql, re.I):
        return [], step_sql

    top = mask_sql(step_sql, nested=True)
    m = re.search(r"\bSELECT\b", top, re.I)
    if not m:
        raise ValueError("Nested WITH has no main SELECT.")

    header, main, htop = step_sql[: m.start()], step_sql[m.start():], top[: m.start()]

    pieces, last = [], 0
    for c in re.finditer(",", htop):
        pieces.append(header[last: c.start()])
        last = c.end()
    pieces.append(header[last:])

    defs = []
    for p in pieces:
        nm = re.match(
            r"\s*(?:WITH\s+)?(\[[^\]]+\]|\w+)\s*(\([^)]*\))?\s*AS\s*(\(.*\))\s*$",
            p,
            re.I | re.S,
        )
        if not nm:
            raise ValueError("Could not parse nested CTE definition.")
        defs.append((nm.group(1).strip("[]"), nm.group(2) or "", nm.group(3)))

    rename = {name: f"{prefix}_{name}" for name, _, _ in defs}

    def ren(text: str) -> str:
        for old, new in rename.items():
            text = re.sub(
                rf"(?<![\w\.\]])(?:\[{re.escape(old)}\]|{re.escape(old)})(?![\w\[])",
                new,
                text,
                flags=re.I,
            )
        return text

    parts = [
        f"{rename[name]}{(' ' + cols) if cols else ''} AS {ren(body)}"
        for name, cols, body in defs
    ]
    return parts, ren(main)

_ITEM = r"(?:\[[^\]]+\]|\w+)(?:\s+(?:ASC|DESC))?"
_SIMPLE_ORDER = re.compile(rf"^{_ITEM}(?:\s*,\s*{_ITEM})*$", re.I)

def _final_order_by(session_id: int, query_id: int):
    last_id = _fetch_ordered_query_ids(session_id, query_id)[-1]
    rows = db.fetch(
        "SELECT sql_query, updated_columns FROM queries WHERE id = %s",
        params=(last_id,),
    )
    if not rows or not rows[0][0]:
        return None

    sql = rows[0][0].strip().rstrip(";")
    top = mask_sql(sql, nested=True)
    ms = list(re.finditer(r"\bORDER\s+BY\b", top, re.I))
    if not ms or re.search(r"\bTOP\b", top, re.I):
        return None

    m = re.match(
        r"^(?P<items>.+?)\s+OFFSET\s+0\s+ROWS$",
        sql[ms[-1].end():].strip(),
        re.I | re.S,
    )
    if not m or not _SIMPLE_ORDER.match(m.group("items").strip()):
        return None

    items = m.group("items").strip()
    try:
        cols = {c.lower() for c in json.loads(rows[0][1] or "[]")}
    except (TypeError, ValueError):
        return None
    names = [
        re.sub(r"\s+(ASC|DESC)$", "", i.strip(), flags=re.I).strip("[]").lower()
        for i in items.split(",")
    ]
    return items if cols and all(n in cols for n in names) else None

def _assemble_chain(session_id: int, query_id: int, alias_prefix: str, used_prefixes: set) -> tuple:
    if alias_prefix in used_prefixes:
        raise ValueError(
            f"Circular or repeated merge detected involving session {session_id} "
            f"(alias prefix '{alias_prefix}' already used)."
        )
    used_prefixes = used_prefixes | {alias_prefix}

    ordered_ids = _fetch_ordered_query_ids(session_id, query_id)
    step_rows = _fetch_step_rows(ordered_ids)
    table_name = _fetch_session_table_name(session_id)

    missing = [qid for qid in ordered_ids if qid not in step_rows]
    if missing:
        raise ValueError(
            f"sql_query missing for query_id(s) {missing} in session {session_id}."
        )

    cte_parts = []
    prev_alias = None

    for n, qid in enumerate(ordered_ids, start=1):
        step = step_rows[qid]
        step_sql = step["sql_query"].strip().rstrip(";")
        step_sql_upper = step_sql.upper()

        if not (step_sql_upper.startswith("SELECT") or step_sql_upper.startswith("WITH")):
            raise ValueError(
                f"Step {n} (query_id={qid}, session={session_id}) is not a SELECT."
            )

        cte_alias = f"{alias_prefix}_{n}_{qid}"

        if step["step_type"] == "join":
            source_session_id = step["source_session_id"]
            if source_session_id is None:
                raise ValueError(
                    f"Step {n} (query_id={qid}, session={session_id}) is step_type='join' "
                    "but has no source_session_id."
                )
            if "{merge}" not in step_sql:
                raise ValueError(
                    f"Step {n} (query_id={qid}, session={session_id}) is a join step but its SQL "
                    "does not contain {merge}."
                )

            source_alias_prefix = f"{alias_prefix}_src_{qid}"
            source_cte_parts, source_final_alias = _assemble_chain(
                source_session_id, -1, source_alias_prefix, used_prefixes
            )
            cte_parts.extend(source_cte_parts)

            step_sql = step_sql.replace("{merge}", source_final_alias)

        if prev_alias is not None:
            step_sql = _replace_table_with_prev(step_sql, table_name, prev_alias)

        lifted, step_sql = _lift_nested_with(step_sql, cte_alias)
        cte_parts.extend(lifted)

        cte_parts.append(f"{cte_alias} AS (\n    {step_sql}\n)")
        prev_alias = cte_alias

    if not cte_parts:
        raise ValueError(f"No CTE parts were assembled for session {session_id}.")

    return cte_parts, prev_alias

def _cte_sql(session_id: int, query_id: int) -> tuple:
    cte_parts, final_alias = _assemble_chain(
        session_id, query_id, alias_prefix="cte", used_prefixes=set()
    )
    return "WITH " + ",\n".join(cte_parts) + "\n", final_alias


def assemble_cte_query(session_id: int, query_id: int) -> str:
    cte_body, final_alias = _cte_sql(session_id, query_id)
    order = _final_order_by(session_id, query_id)
    tail = f"SELECT * FROM {final_alias}" + (f" ORDER BY {order}" if order else "")
    return f"{cte_body}{tail}"


def _search_clause(search, search_columns) -> tuple:
    """Server-side search across the caller's columns. Identifiers are quoted and
    the pattern is parameterised, so neither can alter the statement.

    NVARCHAR(1000), not NVARCHAR(MAX): sqlglot translates MAX to VARCHAR(MAX),
    which MySQL rejects. 1000 characters is plenty for matching a search term.
    """
    term = (search or "").strip()
    if not term or not search_columns:
        return "", ()

    pattern = f"%{term}%"
    conditions, params = [], []
    for col in search_columns:
        if not isinstance(col, str) or not col.strip():
            continue
        conditions.append(
            f"CAST(__r.{execution.quote_tsql(col)} AS NVARCHAR(1000)) LIKE %s"
        )
        params.append(pattern)

    if not conditions:
        return "", ()
    return " WHERE " + " OR ".join(conditions), tuple(params)


def _count_query(session_id: int, query_id: int, search, search_columns) -> tuple:
    cte_body, final_alias = _cte_sql(session_id, query_id)
    where, params = _search_clause(search, search_columns)
    return (
        f"{cte_body}SELECT COUNT(*) AS __total_count FROM {final_alias} AS __r{where}",
        params,
    )


def build_result_query(
    session_id: int,
    query_id: int,
    sort_by: str | None = None,
    sort_dir: str = "asc",
    search: str | None = None,
    search_columns: list | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_total: bool = False,
) -> tuple:
    """Build a single page of the pipeline result.

    Pagination/filtering happens in the database, so a 600k-row session no
    longer ships its whole result set to the client.
    """
    cte_body, final_alias = _cte_sql(session_id, query_id)
    where, params = _search_clause(search, search_columns)

    select_list = "__r.*"
    if include_total:
        # Window functions are evaluated before OFFSET/FETCH, so this is the
        # filtered total while still fetching only the requested page.
        select_list += ", COUNT(*) OVER() AS __total_count"

    sql = f"{cte_body}SELECT {select_list} FROM {final_alias} AS __r{where}"
    if sort_by:
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        sql += f" ORDER BY {execution.quote_tsql(sort_by)} {direction}"
    elif limit is not None:
        # Preserve the step's own ordering; OFFSET/FETCH requires an ORDER BY.
        sql += f" ORDER BY {_final_order_by(session_id, query_id) or '1'}"
    if limit is not None:
        sql += f" OFFSET {int(offset)} ROWS FETCH NEXT {int(limit)} ROWS ONLY"
    return sql, params


def _total_rows(session_id: int, query_id: int, search, search_columns) -> int:
    sql, params = _count_query(session_id, query_id, search, search_columns)
    result = execution.run_for_session(session_id, sql, limit=None, params=params)
    return int(result["rows"][0][0] or 0) if result["rows"] else 0


def run_pipeline_result(
    session_id: int,
    query_id: int,
    limit: int | None = None,
    offset: int = 0,
    sort_by: str | None = None,
    sort_dir: str = "asc",
    search: str | None = None,
    search_columns: list | None = None,
) -> dict:
    """Run the assembled pipeline.

    With `limit=None` this returns the complete result (exports). Otherwise a
    single page is fetched, with `total_count` for the paginated UI.
    """
    if limit is None and not search and not sort_by:
        return execution.run_for_session(
            session_id, assemble_cte_query(session_id, query_id), limit=None
        )

    external = execution.resolve_session_source(session_id) is not None
    include_total = limit is not None and not external
    sql, params = build_result_query(
        session_id,
        query_id,
        sort_by=sort_by,
        sort_dir=sort_dir,
        search=search,
        search_columns=search_columns,
        limit=limit,
        offset=offset,
        include_total=include_total,
    )
    result = execution.run_for_session(session_id, sql, limit=None, params=params)

    if limit is not None:
        if include_total and result["rows"]:
            result["columns"] = result["columns"][:-1]
            result["total_count"] = int(result["rows"][0][-1] or 0)
            result["rows"] = [row[:-1] for row in result["rows"]]
        else:
            # Custom DBs (or an out-of-range page) need an explicit count.
            result["total_count"] = _total_rows(session_id, query_id, search, search_columns)
    return result