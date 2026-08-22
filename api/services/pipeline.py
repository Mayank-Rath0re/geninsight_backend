# services/pipeline.py
"""
Assembles and runs the CTE chain that represents a session's transform
history (was assemble_cte_query / run_pipeline_result in transform_handler.py).

MERGE SUPPORT: a session's step chain can now include 'join' steps
(queries.step_type = 'join', tracked via query_merges). A join step's SQL
references BOTH {prev} (this session's own running output) and {merge}
(the source session's output, frozen at merge time via query_merges.
source_query_id). When assembling the CTE chain, a join step causes the
source session's ENTIRE chain to be recursively assembled and spliced in
as extra CTEs (uniquely aliased to avoid collision with the target
session's own aliases) immediately before the join step's own CTE, which
then resolves {merge} to that spliced-in chain's final alias.

The source session is resolved independently and is never mutated by
this process — merging is a read-only snapshot join, consistent with
"the source session stays alive and usable on its own afterward."

NOTE: `rectify_cte_query`, which previously lived in transform_handler.py,
has been removed. It referenced `self`, `self._pipelines`,
`pipeline.pipeline_trans`, and `pipeline.apply_rectified_cte` — none of
which exist anywhere in this codebase. It was dead code from an earlier,
class-based pipeline design that predates the current session/query-table
schema, and nothing calls it.
"""

from core import db


def _fetch_ordered_query_ids(session_id: int, query_id: int) -> list:
    """
    Returns the ordered list of query_ids for session_id, truncated to
    (and including) query_id. query_id == -1 means "the full chain".
    Raises ValueError on an empty session or an unknown query_id.
    """
    fetch_steps = """
        SELECT query_id
        FROM   session_queries
        WHERE  session_id = %s
        ORDER  BY date_created ASC
    """
    rows = db.fetch(fetch_steps, params=(session_id,))
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
    """Returns {query_id: {"sql_query": ..., "step_type": ...}} for the given ids."""
    if not query_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(query_ids))
    fetch_sql = f"SELECT id, sql_query, step_type FROM queries WHERE id IN ({placeholders})"
    rows = db.fetch(fetch_sql, params=tuple(query_ids))
    return {row[0]: {"sql_query": row[1], "step_type": row[2]} for row in rows}


def _fetch_merge_info(query_id: int):
    """Returns the query_merges row for a join step, or None if not a join step."""
    rows = db.fetch(
        "SELECT source_session_id, source_query_id FROM query_merges WHERE query_id = %s",
        params=(query_id,),
    )
    if not rows:
        return None
    return {"source_session_id": rows[0][0], "source_query_id": rows[0][1]}


def _assemble_chain(session_id: int, query_id: int, alias_prefix: str, used_prefixes: set) -> tuple:
    """
    Recursively assembles the CTE parts for session_id's chain up to
    query_id. Returns (cte_parts: list[str], final_alias: str).

    alias_prefix disambiguates CTE aliases across nested/spliced chains
    (e.g. "cte" for the top-level target session, "src1_cte" for a merged-
    in source session) so a source session's own alias numbering never
    collides with the target session's, even if the same session is
    merged in more than once across different steps.
    """
    if alias_prefix in used_prefixes:
        raise ValueError(
            f"Circular or repeated merge detected involving session {session_id} "
            f"(alias prefix '{alias_prefix}' already used). Merging a session into "
            "itself, directly or transitively, is not supported."
        )
    used_prefixes = used_prefixes | {alias_prefix}

    ordered_ids = _fetch_ordered_query_ids(session_id, query_id)
    step_rows = _fetch_step_rows(ordered_ids)

    missing = [qid for qid in ordered_ids if qid not in step_rows]
    if missing:
        raise ValueError(
            f"sql_query missing for query_id(s) {missing} in session {session_id}. "
            "Rows may have been deleted from the queries table."
        )

    cte_parts = []
    prev_alias = None

    for n, qid in enumerate(ordered_ids, start=1):
        step = step_rows[qid]
        step_sql = step["sql_query"].strip().rstrip(";")
        step_sql_upper = step_sql.upper()

        if not (step_sql_upper.startswith("SELECT") or step_sql_upper.startswith("WITH")):
            raise ValueError(
                f"Step {n} (query_id={qid}, session={session_id}) is not a SELECT — "
                "only SELECT queries (optionally prefixed with WITH) can be embedded in a CTE."
            )

        if n > 1 and "{prev}" not in step_sql:
            raise ValueError(
                f"Step {n} (query_id={qid}, session={session_id}) does not contain {{prev}}. "
                "All steps after the first must reference the upstream CTE output via {{prev}}."
            )

        cte_alias = f"{alias_prefix}_{n}_{qid}"

        if step["step_type"] == "join":
            merge_info = _fetch_merge_info(qid)
            if merge_info is None:
                raise ValueError(
                    f"Step {n} (query_id={qid}, session={session_id}) is marked step_type='join' "
                    "but has no corresponding query_merges row."
                )
            if "{merge}" not in step_sql:
                raise ValueError(
                    f"Step {n} (query_id={qid}, session={session_id}) is a join step but its SQL "
                    "does not contain {{merge}}."
                )

            source_session_id = merge_info["source_session_id"]
            source_query_id = merge_info["source_query_id"] if merge_info["source_query_id"] is not None else -1
            source_alias_prefix = f"{alias_prefix}_src_{qid}"

            source_cte_parts, source_final_alias = _assemble_chain(
                source_session_id, source_query_id, source_alias_prefix, used_prefixes
            )
            cte_parts.extend(source_cte_parts)

            step_sql = step_sql.replace("{merge}", source_final_alias)

        if prev_alias is not None:
            step_sql = step_sql.replace("{prev}", prev_alias)

        cte_parts.append(f"{cte_alias} AS (\n    {step_sql}\n)")
        prev_alias = cte_alias

    if not cte_parts:
        raise ValueError(f"No CTE parts were assembled for session {session_id}.")

    return cte_parts, prev_alias


def assemble_cte_query(session_id: int, query_id: int) -> str:
    print("=== STARTING TRANSFORM (ASSEMBLE CTE)")
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