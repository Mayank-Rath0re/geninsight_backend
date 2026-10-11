# pipeline.py

"""Assembles and runs the CTE chain that represents a session's transform history."""

import re

from core import db


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
        "SELECT id FROM queries WHERE session_id = %s AND is_deleted = 0 ORDER BY created_at ASC",
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
                    "does not contain {{merge}}."
                )

            source_alias_prefix = f"{alias_prefix}_src_{qid}"
            source_cte_parts, source_final_alias = _assemble_chain(
                source_session_id, -1, source_alias_prefix, used_prefixes
            )
            cte_parts.extend(source_cte_parts)

            step_sql = step_sql.replace("{merge}", source_final_alias)

        if prev_alias is not None:
            step_sql = _replace_table_with_prev(step_sql, table_name, prev_alias)

        cte_parts.append(f"{cte_alias} AS (\n    {step_sql}\n)")
        prev_alias = cte_alias

    if not cte_parts:
        raise ValueError(f"No CTE parts were assembled for session {session_id}.")

    return cte_parts, prev_alias


def assemble_cte_query(session_id: int, query_id: int) -> str:
    cte_parts, final_alias = _assemble_chain(
        session_id, query_id, alias_prefix="cte", used_prefixes=set()
    )
    cte_body = "WITH " + ",\n".join(cte_parts)
    return f"{cte_body}\nSELECT * FROM {final_alias};"


def run_pipeline_result(session_id: int, query_id: int, limit: int = 0, offset: int = 0):
    assembled_cte_query = assemble_cte_query(session_id=session_id, query_id=query_id)
    return db.fetch_with_columns(
        assembled_cte_query,
        limit=limit if limit > 0 else None,
        offset=offset if offset > 0 else None,
    )