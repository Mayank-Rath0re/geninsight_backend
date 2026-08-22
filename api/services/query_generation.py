# services/query_generation.py
"""
Two-stage natural-language-to-SQL generation (was
query_generation_instructions / get_sql_query / sanitize_llm_sql, split
across transform_handler.py and helper.py).

Stage 1 (plan): reason about correctness and risk (division by zero,
join fan-out, ambiguous GROUP BY, etc.) as an explicit JSON plan.
Stage 2 (generate): mechanically translate that plan into SQL Server SQL.
"""

import json
import re

from core import json_utils
from services import llm_client


# ─────────────────────────────────────────────────────────────────────────
#  SQL text cleanup (was helper.sanitize_llm_sql)
# ─────────────────────────────────────────────────────────────────────────

def sanitize_llm_sql(sql: str) -> str:
    """
    Light, format-only cleanup of LLM-returned SQL. Deliberately does NOT
    attempt to judge or rewrite query *logic* (e.g. guessing whether a
    column list is "narrowing" and should become SELECT *) — that kind of
    regex-based correctness heuristic caused real bugs where a valid,
    intentional derived-column query was silently rewritten into a plain
    SELECT *, discarding the column the user asked for.

    Correctness (does the output schema match what was planned/requested)
    is instead checked in get_sql_query, by comparing the plan's intended
    column list against the LLM's own reported `columns` — a structural
    comparison of two Python lists, not a regex over SQL text.

    This function only:
      1. Strips markdown fences
      2. Strips trailing semicolons / surrounding whitespace
      3. Fixes the literal typo artifact ``SELECT ,`` -> ``SELECT *,``
    """
    sql = re.sub(r"```(?:sql)?\s*", "", sql)
    sql = re.sub(r"```", "", sql).strip()
    sql = sql.rstrip(";").strip()
    sql = re.sub(r"\bSELECT\s+,", "SELECT *,", sql, flags=re.IGNORECASE)
    return sql


# ─────────────────────────────────────────────────────────────────────────
#  Stage 1: planning
# ─────────────────────────────────────────────────────────────────────────

def query_generation_instructions(
    user_prompt: str,
    tables_info: list,
    table_cols: dict,
    previous_query_data: dict = None,
) -> dict:
    """
    Planning stage. Reasons about *what* the query needs to do and *what
    could go wrong*, before any SQL is written. Returns a structured JSON
    plan that get_sql_query() will mechanically translate into SQL.
    """

    pipeline_position = table_cols.get("pipeline_position", "only")
    prev_cols = table_cols.get("prev_cols")

    def build_tables_block():
        blocks = []
        for t in tables_info:
            kb = json.loads(t["knowledgebase"])
            col_roles = {k: v for k, v in kb.items() if isinstance(v, dict) and "role" in v}
            cols = kb.get("columns_name", [])
            lines = [f"Table [{t['name']}]:", f"  columns: {cols}", "  column roles:"]
            for col, info in col_roles.items():
                lines.append(f"    - [{col}]: role={info['role']} | {info['advice']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    sys_prompt = """You are a query-planning assistant for a SQL Server pipeline. You do NOT write SQL.
Your only job is to produce a structured JSON plan that a downstream generator will translate into SQL.

Think carefully about correctness and safety BEFORE the query is written:
- Division: any ratio/percentage/average-of-ratio must be guarded against divide-by-zero
  (e.g. via NULLIF(denominator, 0)) — call this out explicitly in risk_checks.
- Joins: flag any join key that is not guaranteed unique on one side (risk of row
  duplication / fan-out / cartesian explosion). Recommend aggregating before the join
  if needed.
- NULLs: flag columns used in filters/joins/aggregates that have meaningful null_pct,
  since NULL <> comparisons silently drop rows.
- Unbounded results: if the user's request has no natural filter and the base table(s)
  are large, note that in warnings (do NOT invent a LIMIT unless the user asked for one
  or it's necessary defensively — leave that call to the generator).
- GROUP BY completeness: every non-aggregated selected column must be listed in group_logic.
- Only reference columns that actually exist in the given schemas / {prev} schema.
- If the request cannot be satisfied with available columns, set "feasible": false and
  explain why in "warning" — do not invent columns.
- Column aliases: if any alias in select_columns/new_columns contains a space or special
  character, note in "warning" that the generator must use bracket-delimited identifiers
  (e.g. [Market Cap to Sales Ratio]) — never single quotes, which SQL Server treats as a
  string literal rather than an identifier.

CRITICAL — never produce a GROUP BY with no aggregation:
- group_logic must ONLY be non-empty when aggregate_logic describes at least one real
  aggregate function (SUM, AVG, COUNT, MIN, MAX, etc.) being applied to at least one
  column NOT in group_logic. A GROUP BY that lists every selected column and aggregates
  nothing is functionally identical to returning every raw row — it summarizes nothing
  and is ALWAYS wrong, with no exceptions. If you catch yourself about to put every
  select_columns entry into group_logic, that is the signal you have not actually
  decided what to aggregate yet — go back and decide it.
- Words like "summarize", "overview", "overall performance", "concise table", or
  "high-level" are explicit instructions to REDUCE the row count via aggregation, not
  to return the raw table reshaped. For these requests you must choose a small set of
  grouping dimensions (e.g. a category/type column, or a single overall aggregate with
  no GROUP BY at all if the user wants one row) and aggregate every metric column
  (likes, comments, engagement_rate, reach, etc. — anything numeric that isn't an
  identifier) with an appropriate function (typically AVG or SUM for counts/rates,
  SUM for totals). Never leave this decision to the generator — group_logic and
  aggregate_logic must already reflect the concrete choice.
- If the request is genuinely ambiguous about which dimension to group by, pick the
  single most analytically meaningful low-cardinality dimension column available
  (per the column roles provided) and note the choice in "suggestions" — do not default
  to grouping by every column, and never include high-cardinality/identifier-role
  columns (e.g. post_id, account_id) in group_logic.
- Optimization: note any concrete, high-impact performance improvement for THIS step's
  query specifically — e.g. pushing a filter earlier, aggregating on fewer columns,
  avoiding a function wrapped around a filtered/joined column (non-sargable), or
  computing an aggregate once instead of repeating the same expression. Scope this to
  the single SELECT this step produces; it is not about the multi-step pipeline this
  query later becomes part of, and it is not an instruction about CTEs — CTE chaining
  across pipeline steps is handled separately, outside this step's query, so do not
  suggest avoiding or restructuring CTEs here.

Respond with ONLY a single raw JSON object — no markdown fences, no preamble, no trailing text.
The response must be directly parseable by json.loads().
"""

    prev_schema_note = (
        f"CURRENT {{prev}} SCHEMA (only these columns are available from {{prev}}):\n  {prev_cols}\n"
        "If a needed column is not in this list, it must come from a JOIN to a base table."
        if prev_cols else
        "No {prev} in this step — this is the first/only step. Query base tables directly."
    )

    prev_history_note = ""
    if previous_query_data is not None:
        prev_history_note = f"""
PREVIOUS STEP CONTEXT:
  Summary: {previous_query_data.get("summary")}
  Previous SQL: {previous_query_data.get("sql_query")}
  Columns after previous step: {previous_query_data.get("updated_columns")}
"""

    user_prompt_full = f"""
PIPELINE POSITION: {pipeline_position}
  first  → no {{prev}}, query base tables directly
  middle → {{prev}} is the primary source, do not select from base tables as primary source
  only   → standalone, query base tables directly

{prev_schema_note}
{prev_history_note}

BASE TABLES:
{build_tables_block()}

USER REQUEST: {user_prompt}

Return exactly this JSON structure (fill in every key; use empty list/string/null where not applicable):
{{
  "feasible": true,
  "tables_needed": ["table or {{prev}} names actually required"],
  "column_plan": {{
    "select_columns": ["exact output columns, in order, including derived/aggregate aliases"],
    "new_columns": [
      {{"name": "col_name", "expression_intent": "plain-English description of the formula, not SQL"}}
    ]
  }},
  "join_logic": {{
    "needed": false,
    "joins": [
      {{"left": "...", "right": "...", "on": "...", "type": "INNER|LEFT",
        "fanout_risk": "none|possible - explanation"}}
    ]
  }},
  "filter_logic": "plain-English description of WHERE conditions, or empty string",
  "aggregate_logic": "plain-English description of aggregate functions and their targets, or empty string",
  "group_logic": ["columns required in GROUP BY given the select/aggregate plan"],
  "sort_logic": "plain-English description of ORDER BY, or empty string",
  "risk_checks": [
    {{"risk": "division_by_zero|null_unsafe_join|fanout|ambiguous_group_by|unbounded_result|other",
      "detail": "what specifically triggers this and how to guard against it in the SQL"}}
  ],
  "warning": "any concern the generator must respect (e.g. missing columns, alias quoting), or empty string",
  "suggestions": "optional follow-up ideas for the user, or empty string",
  "optimization": "one concise, high-impact performance suggestion for this step's query, scoped to a single SELECT (not the multi-step pipeline, not CTE structure), or empty string"
}}"""

    chars = len(sys_prompt) + len(user_prompt_full)
    print(f"--- QUERY PLANNING CALLED: {chars} characters or {chars/4} tokens roughly")

    raw = llm_client.ask_llm(sys_prompt, user_prompt_full)
    plan = json_utils.extract_json_object(raw)

    print("-- Query Plan Generated: ", json.dumps(plan, indent=2))
    return plan


# ─────────────────────────────────────────────────────────────────────────
#  Stage 2: SQL generation from the plan
# ─────────────────────────────────────────────────────────────────────────

def get_sql_query(
    user_prompt: str,
    tables_info: list,
    previous_query_data: dict = None,
) -> dict:

    table_cols = {
        "pipeline_position": "only" if previous_query_data is None else "middle",
        "prev_cols": None if previous_query_data is None else previous_query_data["updated_columns"],
    }

    # ── Stage 1: plan / risk analysis ──
    plan = query_generation_instructions(
        user_prompt=user_prompt,
        tables_info=tables_info,
        table_cols=table_cols,
        previous_query_data=previous_query_data,
    )

    if plan.get("feasible") is False:
        return {
            "sql": "-- Error: Requested fields not found in schema.",
            "summary": plan.get("warning") or "Request not feasible with available columns.",
            "columns": [],
        }

    # ── Stage 2: mechanical SQL generation from the plan ──
    sys_prompt = f"""
You are a SQL Server query generation assistant. You are given a structured PLAN
(already reasoned about correctness and risk) and must translate it into a single
valid SQL Server SELECT query. Do NOT re-plan or second-guess the plan's logic —
only implement it correctly and safely in SQL.

━━━ PIPELINE POSITION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{table_cols["pipeline_position"]}
  first  → No {{prev}}. Query base tables directly.
  middle → Use {{prev}} as your primary source. Never query base tables as primary source.
  only   → Standalone. Query base tables directly.

━━━ RISK CHECKS TO IMPLEMENT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Apply every item below as a concrete SQL construct (e.g. divide_by_zero →
wrap the denominator in NULLIF(denominator, 0); fanout → aggregate before
joining or use DISTINCT as appropriate; null_unsafe_join → use appropriate
NULL-safe conditions or explicit filters):
{json.dumps(plan.get("risk_checks", []), indent=2)}

━━━ OPTIMIZATION GUIDANCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Apply this to the single SELECT you are writing, if it doesn't conflict with
the risk checks or hard rules above. This is scoped to this one query only —
it is not an instruction about the multi-step pipeline this query becomes
part of, and it is not about CTE structure (CTE chaining across pipeline
steps happens outside this query and is not your concern here):
{plan.get("optimization") or "none"}

━━━ HARD RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Only SELECT. Never DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, CREATE, GRANT.
2. Only reference columns that exist in {{prev}} schema (middle) or base tables (first/only).
3. String literals (values, in WHERE/comparisons) use single quotes.
   Column ALIASES that contain spaces or special characters MUST use
   bracket-delimited identifiers — e.g. AS [Market Cap to Sales Ratio] —
   NEVER single quotes for an alias (SQL Server treats 'text' as a string
   literal, not an identifier, and the query will not mean what you intend).
   Aliases with no spaces/special characters do not need brackets.
4. Multi-table queries must use table aliases.
5. No trailing semicolon.
6. Every non-aggregated SELECT column must appear in GROUP BY if any aggregate is used.
7. Do not invent columns not present in the plan's tables_needed schemas.
8. The "columns" field you return MUST exactly match plan.column_plan.select_columns,
   in the same order — this is the single source of truth for the output schema,
   and it is checked programmatically after you respond.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return only a raw JSON object — no markdown, no fences:
{{
  "sql":     "<raw SQL>",
  "summary": "<one line: what structurally changed>",
  "columns": ["col1", "col2", ...]
}}
"""

    def table_string(table_info: dict):
        return f"""\n
        Table Name: {table_info["name"]}
            metadata: {table_info["metadata"]}
            knowledge_base: {table_info["knowledgebase"]}\n
"""

    sql_user_prompt = "PLAN TO IMPLEMENT:\n" + json.dumps(plan, indent=2)

    if previous_query_data is not None:
        sql_user_prompt += f"""

Previous Query Info:
    Summary of Action: {previous_query_data["summary"]}
    SQL Query Generated: {previous_query_data["sql_query"]}
    Latest Columns (Post Query Execution): {previous_query_data["updated_columns"]}
"""

    sql_user_prompt += "\nTables Info:\n"
    for table in tables_info:
        sql_user_prompt += table_string(table)

    sql_user_prompt += f"""
User Prompt (original, for reference only — follow the PLAN above): {user_prompt}
REMINDER: Use {{prev}} as the FROM source when pipeline_position is 'middle'. SELECT only the plan's select_columns unless plan indicates otherwise.
"""

    characters = len(sql_user_prompt) + len(sys_prompt)
    print(f"--- SQL QUERY GENERATION CALLED: {characters} characters or {characters/4} tokens roughly")

    raw = llm_client.ask_llm(sys_prompt, sql_user_prompt)
    print("-- SQL Query Generated: ", raw)
    result = json_utils.extract_json_object(raw)

    sql = sanitize_llm_sql(result["sql"])

    # ── Structural correctness check ──
    # The plan's column_plan.select_columns is the source of truth for what
    # the output schema should be. If the model's self-reported "columns"
    # don't match the plan, we don't try to rewrite the SQL — we just trust
    # the plan's column list for pipeline schema tracking, since the plan
    # was reasoned about explicitly.
    planned_cols = plan.get("column_plan", {}).get("select_columns") or result["columns"]
    reported_cols = result["columns"]

    if set(reported_cols) != set(planned_cols):
        print(
            f"-- WARNING: generated SQL columns {reported_cols} do not match "
            f"planned columns {planned_cols}. Using planned columns for pipeline "
            f"schema tracking; SQL itself is left as generated (not rewritten)."
        )
        columns_out = planned_cols
    else:
        columns_out = reported_cols

    return {
        "sql": sql,
        "summary": result["summary"],
        "columns": columns_out,
    }
