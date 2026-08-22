# services/merge_generation.py
"""
Two-stage natural-language-to-SQL generation for MERGE (join) steps —
sibling to services/query_generation.py, same plan-then-generate pattern,
same LLM trust model (no human confirmation step, consistent with how
/transform already executes LLM-generated SQL directly).

A merge step combines two upstream sources within a single session's CTE
chain:
  {prev}   — the target session's own pipeline output so far
  {merge}  — the source session's pipeline output (its own CTE chain,
             spliced in by services/pipeline.py), frozen at merge time

Both stages here are read-only/SELECT-only, same as the transform flow.
Only SELECT is ever produced — enforced the same way (hard rule in the
generation prompt + the same non-SELECT guard in pipeline.py's CTE
assembly applies to merge steps too).
"""

import json

from core import json_utils
from services import llm_client
from services.query_generation import sanitize_llm_sql  # same format-only cleanup, reused as-is


# ─────────────────────────────────────────────────────────────────────────
#  Stage 1: planning — decide join keys, join type, fan-out risk
# ─────────────────────────────────────────────────────────────────────────

def merge_plan_instructions(
    target_schema_cols: list,
    target_context: dict,
    source_schema_cols: list,
    source_context: dict,
    user_hint: str = "",
) -> dict:
    """
    Planning stage for a merge/join. Given the CURRENT output schema of
    the target session ({prev}) and the CURRENT output schema of the
    source session ({merge}) — plus each side's knowledge base for
    semantic context — decides join key(s), join type, and flags fan-out
    risk, exactly the way query_generation's planning stage flags risk
    for ordinary transforms.
    """

    sys_prompt = """You are a join-planning assistant for a SQL Server pipeline. You do NOT write SQL.
Your only job is to produce a structured JSON plan describing how to join two already-computed
result sets, named {prev} (the target/primary side) and {merge} (the side being merged in).

Think carefully before proposing a join:
- Identify the most semantically correct join key(s) by matching column names, roles, and
  descriptions across both sides (e.g. customer_id <-> cust_id, region <-> region_name) — do not
  assume identical column names are required, but do not guess wildly either; if no confident key
  exists, set "feasible": false and explain why in "warning".
- Prefer keys marked role=identifier on at least one side. A join on a low-cardinality
  dimension/category column is valid but must be flagged with fanout_risk since it is far more
  likely to duplicate rows on both sides.
- join_type: choose LEFT if the target/{prev} side represents the "primary" entity and the
  {merge} side is supplementary/optional data (rows in {prev} should be preserved even with no
  match). Choose INNER only when unmatched rows on either side are meaningless for this merge.
  Choose FULL only if the user's hint explicitly asks to keep unmatched rows from both sides.
  Choose RIGHT only if the user's hint explicitly frames {merge} as primary.
- Only propose columns that actually exist in the given schemas. Never invent columns.
- If both sides have overlapping non-key column names, note this in "warning" so the generator
  knows to alias/prefix them to avoid an ambiguous-column SQL error.
- fanout_risk: "none" only if the join key is unique on at least one side. Otherwise
  "possible - <reason>".

Respond with ONLY a single raw JSON object — no markdown fences, no preamble, no trailing text.
The response must be directly parseable by json.loads().
"""

    user_prompt = f"""
TARGET SIDE — {{prev}} (current pipeline output of the session being merged INTO):
  columns: {target_schema_cols}
  knowledge_base: {json.dumps(target_context, indent=2)}

SOURCE SIDE — {{merge}} (current pipeline output of the session being merged FROM):
  columns: {source_schema_cols}
  knowledge_base: {json.dumps(source_context, indent=2)}

USER HINT (optional, may be empty — free-text guidance on how to join, e.g. "join on customer_id"
or "keep all rows from both"): {user_hint or "(none provided)"}

Return exactly this JSON structure:
{{
  "feasible": true,
  "join_type": "INNER|LEFT|RIGHT|FULL",
  "join_keys": [
    {{"prev_column": "...", "merge_column": "...", "confidence": "high|medium|low"}}
  ],
  "fanout_risk": "none|possible - explanation",
  "overlapping_columns": ["column names present on both sides that need aliasing/prefixing"],
  "select_columns": ["exact output columns, in order — every column from {{prev}} plus every non-key, non-overlapping column from {{merge}}; prefix overlapping columns e.g. merge_<col>"],
  "warning": "any concern the generator must respect, or empty string",
  "join_summary": "one short human-readable sentence, e.g. 'Joined with Region.csv on region_id'"
}}"""

    raw = llm_client.ask_llm(sys_prompt, user_prompt)
    plan = json_utils.extract_json_object(raw)
    print("-- Merge Plan Generated: ", json.dumps(plan, indent=2))
    return plan


# ─────────────────────────────────────────────────────────────────────────
#  Stage 2: SQL generation from the merge plan
# ─────────────────────────────────────────────────────────────────────────

def get_merge_sql(
    target_schema_cols: list,
    target_context: dict,
    source_schema_cols: list,
    source_context: dict,
    user_hint: str = "",
) -> dict:
    """
    Runs the plan stage, then mechanically generates a single SELECT that
    joins {prev} and {merge}. Returns the same shape as
    query_generation.get_sql_query: {"sql", "summary", "columns"}, plus
    merge-specific plan metadata the caller needs for query_merges
    (join_type, join_summary).
    """

    plan = merge_plan_instructions(
        target_schema_cols=target_schema_cols,
        target_context=target_context,
        source_schema_cols=source_schema_cols,
        source_context=source_context,
        user_hint=user_hint,
    )

    if plan.get("feasible") is False:
        return {
            "sql": "-- Error: Requested fields not found in schema.",
            "summary": plan.get("warning") or "Merge not feasible with available columns.",
            "columns": [],
            "join_type": None,
            "join_summary": None,
        }

    sys_prompt = f"""
You are a SQL Server query generation assistant. You are given a structured JOIN PLAN
(already reasoned about join keys, join type, and fan-out risk) and must translate it into a
single valid SQL Server SELECT query that joins two named subqueries: {{prev}} and {{merge}}.
Do NOT re-plan or second-guess the plan's join key/type choices — only implement them correctly.

━━━ HARD RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. FROM {{prev}} AS t  <JOIN TYPE> JOIN {{merge}} AS m ON <join keys from the plan>
   (use aliases exactly named t and m).
2. Only SELECT. Never DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, CREATE, GRANT.
3. Only reference columns present in the given {{prev}}/{{merge}} column lists.
4. Every column in plan.overlapping_columns must be selected with an unambiguous alias
   (e.g. m.<col> AS [merge_<col>]) to avoid a duplicate-column-name error.
5. Column ALIASES with spaces/special characters MUST use bracket identifiers, e.g.
   AS [Merge Total]. Never single quotes for an alias.
6. The join condition(s) must come from plan.join_keys exactly — do not invent additional
   join keys or drop any listed one.
7. No trailing semicolon.
8. The "columns" field you return MUST exactly match plan.select_columns, in the same order.

━━━ JOIN PLAN ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(plan, indent=2)}

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return only a raw JSON object — no markdown, no fences:
{{
  "sql":     "<raw SQL, using literal {{prev}} and {{merge}} as FROM/JOIN sources>",
  "summary": "<one line: what this merge produced>",
  "columns": ["col1", "col2", ...]
}}
"""

    user_prompt = f"""
{{prev}} columns: {target_schema_cols}
{{merge}} columns: {source_schema_cols}
User hint (for reference only — follow the PLAN above): {user_hint or "(none provided)"}
"""

    raw = llm_client.ask_llm(sys_prompt, user_prompt)
    print("-- Merge SQL Generated: ", raw)
    result = json_utils.extract_json_object(raw)

    sql = sanitize_llm_sql(result["sql"])

    if "{prev}" not in sql or "{merge}" not in sql:
        raise ValueError(
            "Generated merge SQL is missing a required placeholder "
            "({prev} and/or {merge}) — cannot be spliced into the pipeline."
        )

    planned_cols = plan.get("select_columns") or result["columns"]
    reported_cols = result["columns"]
    if set(reported_cols) != set(planned_cols):
        print(
            f"-- WARNING: generated merge SQL columns {reported_cols} do not match "
            f"planned columns {planned_cols}. Using planned columns for pipeline schema tracking."
        )
        columns_out = planned_cols
    else:
        columns_out = reported_cols

    return {
        "sql": sql,
        "summary": result["summary"],
        "columns": columns_out,
        "join_type": plan.get("join_type"),
        "join_summary": plan.get("join_summary"),
    }