import re
import pandas as pd
import json
from typing import Dict, Any, Optional, List
import llm_handler
import helper
import db_handler


def assemble_cte_query(session_id: int, query_id: int) -> str:
 
    # 1. Fetch all steps for session in pipeline order
    print("=== STARTING TRANSFORM (ASSEMBLE CTE)")
    fetch_steps = """
        SELECT query_id
        FROM   session_queries
        WHERE  session_id = %s
        ORDER  BY date_created ASC
    """
    rows = db_handler.run_fetch_query(fetch_steps, params=(session_id,))
 
    if not rows:
        raise ValueError(
            f"Session {session_id} is empty or does not exist."
        )
 
    all_query_ids = [row[0] for row in rows]
    print("====== SESSION HISTORY: ")
 
    # 2. Slice pipeline up to target query_id
    if query_id == -1:
        ordered_ids = all_query_ids
    else:
        try:
            cut = all_query_ids.index(query_id)
            ordered_ids = all_query_ids[: cut + 1]
        except ValueError:
            raise ValueError(
                f"query_id={query_id} not found in session {session_id}. "
                f"Pipeline contains: {all_query_ids}"
            )
 
    # 3. Fetch SQL bodies for all required steps
    placeholders = ", ".join(["%s"] * len(ordered_ids))
    fetch_sql    = f"SELECT id, sql_query FROM queries WHERE id IN ({placeholders})"
    sql_rows     = db_handler.run_fetch_query(fetch_sql, params=tuple(ordered_ids))
    sql_map      = {row[0]: row[1] for row in sql_rows}
    print(sql_map)
 
    # Validate — every step in the slice must have a SQL body
    missing = [qid for qid in ordered_ids if qid not in sql_map]
    if missing:
        raise ValueError(
            f"sql_query missing for query_id(s) {missing}. "
            "Rows may have been deleted from the queries table."
        )
 
    # 4. Assemble CTE chain
    cte_parts = []
    prev      = None
 
    for n, qid in enumerate(ordered_ids, start=1):
        step_sql = sql_map[qid].strip().rstrip(";")
 
        # Safety: only SELECT statements can live inside a CTE
        if not step_sql.upper().startswith("SELECT"):
            raise ValueError(
                f"Step {n} (query_id={qid}) is not a SELECT — "
                "only SELECT queries can be embedded in a CTE."
            )
 
        # Steps 2+ must chain via {prev}; if missing the pipeline is broken
        if n > 1 and "{prev}" not in step_sql:
            raise ValueError(
                f"Step {n} (query_id={qid}) does not contain {{prev}}. "
                "All steps after the first must reference the upstream "
                "CTE output via {{prev}}."
            )
 
        cte_alias = f"cte_{n}_{qid}"
 
        if prev is not None:
            step_sql = step_sql.replace("{prev}", prev)
 
        cte_parts.append(f"{cte_alias} AS (\n    {step_sql}\n)")
        prev = cte_alias
 
    if not cte_parts:
        raise ValueError("No CTE parts were assembled.")
 
    cte_body  = "WITH " + ",\n".join(cte_parts)
    final_sql = f"{cte_body}\nSELECT * FROM {prev};"
 
    return final_sql
 
 
def run_pipeline_result(sessionId: int, queryId: int, limit: int = 0, offset: int = 0):
    assembled_cte_query = assemble_cte_query(session_id=sessionId, query_id=queryId)
    output = db_handler.run_fetch_query_with_columns(
        assembled_cte_query,
        limit=limit if limit > 0 else None,
        offset=offset if offset > 0 else None,
    )
    return output

def extract_metadata(filepath: str) -> str:
    meta: Dict[str, Any] = {}
    df = helper.get_dataframe(filepath)
    for col in df.columns:
        meta[col] = {
            "dtype":         str(df[col].dtype),
            "null_count":    int(df[col].isnull().sum()),
            "null_pct":      round(df[col].isnull().mean() * 100, 2),
            "unique_count":  int(df[col].nunique()),
            "sample_values": df[col].dropna().unique()[:5].tolist(),
        }
        if pd.api.types.is_numeric_dtype(df[col]):
            meta[col]["min"]  = float(df[col].min())
            meta[col]["max"]  = float(df[col].max())
            meta[col]["mean"] = round(float(df[col].mean()), 4)
    return json.dumps(meta, indent=2, default=str)


def classify_column_roles(df: pd.DataFrame, metadata: str) -> str:
    """
    Classify each column into an analytical role so the LLM knows how to
    use it in GROUP BY and aggregations.
    """
    roles = {}
    n_rows = len(df)

    metadata = json.loads(metadata)
    for col, meta in metadata.items():
        dtype       = meta["dtype"]
        unique_ct   = meta["unique_count"]
        cardinality = unique_ct / n_rows if n_rows > 0 else 0
        col_lower   = col.lower()

        is_id_name = any(
            col_lower == pat or col_lower.endswith(pat)
            for pat in ("id", "_id", "no", "_no", "code", "key", "uuid", "num")
        )
        if is_id_name and cardinality > 0.5:
            roles[col] = {
                "role":   "identifier",
                "advice": "Do NOT use in GROUP BY or aggregate. Omit from SELECT when aggregating.",
            }
            continue

        if (
            "date" in col_lower or "time" in col_lower or "dt" in col_lower
            or dtype.startswith("datetime")
        ):
            roles[col] = {
                "role":   "date",
                "advice": "Use in GROUP BY for time-series. Can use DATE(), MONTH(), YEAR().",
            }
            continue

        if dtype in ("int64", "float64", "int32", "float32"):
            if unique_ct <= 20:
                roles[col] = {
                    "role":   "dimension",
                    "advice": "Low-cardinality numeric — treat as GROUP BY dimension.",
                }
            else:
                roles[col] = {
                    "role":   "metric",
                    "advice": (
                        f"Aggregate with SUM(), AVG(), MAX(), MIN(). "
                        f"Range: {meta.get('min')} to {meta.get('max')}. "
                        "Do NOT put in GROUP BY unless explicitly requested."
                    ),
                }
            continue

        if dtype == "object":
            if cardinality < 0.05:
                roles[col] = {
                    "role":   "dimension",
                    "advice": (
                        f"Categorical dimension with {unique_ct} unique values. "
                        f"Ideal for GROUP BY. Sample: {meta['sample_values'][:3]}"
                    ),
                }
            else:
                roles[col] = {
                    "role":   "text",
                    "advice": "High-cardinality text. Avoid GROUP BY unless filtering to specific values.",
                }
            continue

        roles[col] = {"role": "unknown", "advice": "Use with caution."}
    return json.dumps(roles, indent=2, default=str)


# ─────────────────────────────────────────────────────────────────────────────
#  Knowledge Base
# ─────────────────────────────────────────────────────────────────────────────

def build_knowledge_base(
    df: pd.DataFrame,
    metadata: str,
    table_name: str,
    roles: str,
) -> str:
    describe_str = df.describe(include="all").to_string()
    sample_str   = df.head(5).to_string(index=False)
    cols         = list(df.columns)

    system = (
        "You are a senior data analyst. "
        "Respond with ONLY a single raw JSON object — no markdown fences, "
        "no explanation, no preamble, no trailing text. "
        "The response must be directly parseable by json.loads()."
    )

    user = f"""
SCHEMA METADATA:
{metadata}

STATISTICAL SUMMARY:
{describe_str}

SAMPLE ROWS (first 5):
{sample_str}

Return exactly this JSON structure (fill in values, keep all keys):
{{
  "columns_name": {json.dumps(cols)},
  "table_name": "{table_name}",
  "domain": "infer from data e.g. sales, finance, healthcare",
  "description": "2-3 sentence plain-English description of the dataset",
  "row_count": {df.shape[0]},
  "column_count": {df.shape[1]},
  "key_columns": ["list the most analytically important columns"],
  "date_columns": ["columns that are or look like dates/timestamps"],
  "numeric_columns": ["all numeric columns"],
  "categorical_columns": ["all categorical/string columns"],
  "data_quality_notes": "brief note on nulls, outliers, or data issues",
  "potential_analyses": ["3-5 analysis ideas this data supports"]
}}"""
    chars = len(system) + len(user)
    print(f"---AI Called for Build_Knowledge_Base: {chars} characters or {chars/4} tokens roughly")

    raw = llm_handler.ask_llm(system, user)

    kb_dict = helper.extract_json_object(raw)        # already a dict
    kb_dict["column_roles"] = json.loads(roles)       # roles is a JSON string, parse it

    return json.dumps(kb_dict)

# ─────────────────────────────────────────────────────────────────────────────
#  Basic Transform
# ─────────────────────────────────────────────────────────────────────────────

def basic_transform(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Standard auto-transforms run before any user-requested transforms:
      1. Remove exact duplicate rows
      2. Drop columns where >80% values are null
      3. Fill numeric nulls with column median
      4. Fill categorical nulls with 'Unknown'
      5. Parse date columns identified in knowledge base (skipped if kb absent)
    """
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.replace(r'[^A-Za-z0-9]+', '_', regex=True)  # replace non-alphanumeric chars with _
        .str.strip('_')                                  # remove leading/trailing underscores
    )

    before = len(df)
    df.drop_duplicates(inplace=True)
    print(f"Duplicates removed: {before - len(df)}")

    null_pct  = df.isnull().mean()
    drop_cols = null_pct[null_pct > 0.8].index.tolist()
    if drop_cols:
        df.drop(columns=drop_cols, inplace=True)
    print(f"High-null columns dropped: {drop_cols or 'none'}")

    num_cols   = df.select_dtypes(include="number").columns.tolist()
    filled_num = []
    for col in num_cols:
        if df[col].isnull().sum() > 0:
            median_val = df[col].median()
            if pd.notnull(median_val):
                df[col] = df[col].fillna(median_val)
            filled_num.append(col)
    print(f"Numeric nulls filled: {filled_num or 'none'}")

    try:
        cat_cols = df.select_dtypes(include=["string", "category"]).columns.tolist()
    except Exception:
        cat_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()

    filled_cat = []
    for col in cat_cols:
        if df[col].isnull().sum() > 0:
            df[col] = df[col].fillna("Unknown")
            filled_cat.append(col)
    print(f"Categorical nulls filled: {filled_cat or 'none'}")

    return df


# ─────────────────────────────────────────────────────────────────────────────
#  SQL Generation
# ─────────────────────────────────────────────────────────────────────────────

def build_sys_prompt(tables_info: dict, table_cols: dict) -> str:

    # Normalize: single table dict → list
    tables = tables_info if isinstance(tables_info, list) else [tables_info]

    def build_tables_block():
        blocks = []
        for t in tables:
            kb = json.loads(t["knowledgebase"])
            col_roles = {k: v for k, v in kb.items() if isinstance(v, dict) and "role" in v}
            cols = kb.get("columns_name", [])
            lines = [
                f"Table [{t['name']}]:",
                f"  columns: {cols}",
                f"  column roles:",
            ]
            for col, info in col_roles.items():
                lines.append(f"    - [{col}]: role={info['role']} | {info['advice']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def build_prev_schema_block():
        prev_cols = table_cols.get("prev_cols")
        if not prev_cols:
            return ""
        return f"""CURRENT {{prev}} SCHEMA (columns available in this pipeline step):
  {prev_cols} 
  Reference ONLY these columns from {{prev}}. 
  If you need a column not listed here, JOIN the base table that contains it."""

    pipeline_position = table_cols.get("pipeline_position", "only")
    table_names = [t["name"] for t in tables]

    return f"""
You are a SQL Server query generation assistant. Convert a natural language prompt into a valid SQL Server SELECT query.

━━━ PIPELINE POSITION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{pipeline_position}
  first  → No {{prev}}. Query base tables directly.
  middle → Use {{prev}} as your primary source. Never query base tables as primary source.
  only   → Standalone. Query base tables directly.

━━━ BASE TABLES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{build_tables_block()}

{build_prev_schema_block()}

━━━ QUERY PATTERNS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use exactly one pattern per query.

A) Filter / Order       → SELECT * FROM {{prev}} WHERE ... / ORDER BY ...
B) Derived column       → SELECT *, <expr> AS new_col FROM {{prev}}
C) Join new table       → SELECT p.*, t2.col_a, t2.col_b
                               FROM {{prev}} p JOIN base_table t2 ON p.key = t2.key
                          Use when user needs columns absent from {{prev}}.
                          Always qualify columns with aliases across JOINs.
D) Aggregation          → SELECT dim1, SUM(metric) AS total FROM {{prev}} GROUP BY dim1
                          Every non-aggregate SELECT column must appear in GROUP BY.
E) First / Only step    → SELECT * FROM base_table WHERE ...
                          SELECT t1.*, t2.col FROM base_table t1 JOIN base_table2 t2 ON ...

Column not in {{prev}} but in a base table → use Pattern C.
Column not found anywhere → return the error string (see below).

━━━ HARD RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Only SELECT. Never DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, CREATE, GRANT.
2. Only reference columns that exist in {{prev}} schema (middle) or base tables (first/only).
3. String literals use single quotes. Numeric values unquoted.
4. Multi-table queries must use table aliases.
5. No trailing semicolon.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return only a raw JSON object — no markdown, no fences:
{{
  "sql":     "<raw SQL>",
  "summary": "<one line: what structurally changed — rows filtered, column added, joined with X, aggregated by Y>",
  "columns": ["col1", "col2", ...]
}}

columns must reflect output schema exactly:
  A → same as prev_cols
  B → prev_cols + [new_col]
  C → prev_cols + [joined cols]
  D → only the GROUP BY + aggregate cols
  E → all columns from base table(s) used

If the request cannot be fulfilled from available columns:
  -- Error: Requested fields not found in schema.

AVAILABLE TABLES: {table_names}
"""

def table_string(table_info: dict):
    return f"""\n
        Table Name: {table_info["name"]}
            metadata: {table_info["metadata"]}
            knowledge_base: {table_info["knowledgebase"]}\n
""" 

def get_sql_query(
    user_prompt: str,
    tables_info: list,
    previous_query_data: dict = None,
) -> dict:  # ← now returns dict, not str

    table_cols = {
        "pipeline_position": "only" if previous_query_data is None else "middle",
        "prev_cols": None if previous_query_data is None else previous_query_data["updated_columns"]
    }

    sys_prompt = build_sys_prompt(tables_info, table_cols)

    sql_user_prompt = ""
    if previous_query_data is not None:
        sql_user_prompt = f"""
Previous Query Info:
    Summary of Action: {previous_query_data["summary"]}
    SQL Query Generated: {previous_query_data["sql_query"]}
    Latest Columns (Post Query Execution): {previous_query_data["updated_columns"]}
"""
    sql_user_prompt += "\nTables Info:\n"
    for table in tables_info:
        sql_user_prompt += table_string(table)

    sql_user_prompt += f"""
User Prompt: {user_prompt}
REMINDER: Use {{prev}} as the FROM source. SELECT * unless aggregating.
"""
    characters = len(sql_user_prompt) + len(sys_prompt)
    print(f"--- SQL QUERY GENERATION CALLED: {characters} characters or {characters/4} tokens roughly")
    raw = llm_handler.ask_llm(sys_prompt, sql_user_prompt)
    print("-- SQL Query Generated: ", raw)
    result = helper.extract_json_object(raw)  # already have this helper

    return {
        "sql":     helper.sanitize_llm_sql(result["sql"]),
        "summary": result["summary"],
        "columns": result["columns"]
    }

# ─────────────────────────────────────────────────────────────────────────────
#  CTE Rectification
# ─────────────────────────────────────────────────────────────────────────────

def rectify_cte_query(
        self,
        table_columns: Dict[str, List[str]],
        final_cte_query: str,
        prompt_history: Optional[List[str]] = None,
        owner_table: Optional[str] = None,
    ) -> str:
        """
        Send the assembled CTE query to the LLM for validation and correction,
        then write any corrections back into the owning pipeline's pipeline_trans
        so that future build_cte_string() calls are already correct.

        owner_table : str | None
            The table whose pipeline produced final_cte_query.  When provided,
            write-back is scoped to ONLY that pipeline — preventing cross-table
            query corruption where pipeline B would otherwise receive corrections
            intended for pipeline A (they share the same un-prefixed CTE alias
            names, so alias lookup would match incorrectly).
            When None (legacy / cross-table path), write-back is attempted on
            all registered pipelines (old behaviour, safe only when aliases are
            guaranteed unique across tables — they are not in single-table mode).

        Returns the corrected SQL string.
        Skips LLM call when total active steps <= 1 (nothing to cross-validate).
        """
        total_active = sum(
            sum(1 for s in pl.pipeline_trans if s.is_active)
            for pl in self._pipelines.values()
        )
        if total_active <= 1:
            return final_cte_query

        sys_prompt = """You are an expert SQL SERVER specialist focused on CTE query correctness.

Given:
- Source table schemas (table name + columns)
- The sequence of transformation prompts the user requested
- The assembled CTE query

Your job:
1. Verify every column reference exists in the CTE step that introduces it.
2. Check GROUP BY completeness (only_full_group_by mode is active).
3. Ensure no step narrows the column set in a way that breaks a later step.
   If a step uses SELECT with specific columns (not SELECT *), verify the
   next step does not reference columns that were dropped.
4. Fix alias shadowing and syntax issues.
5. If the query is already correct, return it unchanged.

Return ONLY the raw corrected SQL query. No markdown, no explanation, no preamble."""

        prompt_parts: List[str] = []

        for table_name, pipeline in self._pipelines.items():
            cols           = table_columns.get(table_name, [])
            active_queries = [s.query for s in pipeline.pipeline_trans if s.is_active]
            prompt_parts.append(f"TABLE: `{table_name}`")
            prompt_parts.append(f"COLUMNS: {cols}")
            prompt_parts.append(f"STEP QUERIES: {active_queries}")
            prompt_parts.append("")

        if prompt_history:
            prompt_parts.append("USER PROMPT SEQUENCE (in order):")
            for i, p in enumerate(prompt_history, 1):
                prompt_parts.append(f"  {i}. {p}")
            prompt_parts.append("")

        prompt_parts.append("--- ASSEMBLED CTE QUERY TO VALIDATE ---")
        prompt_parts.append(final_cte_query)

        user_prompt     = "\n".join(prompt_parts)
        corrected_query = llm_handler.ask_llm(sys_prompt, user_prompt)

        corrected_query = re.sub(r"```(?:sql)?\s*", "", corrected_query)
        corrected_query = re.sub(r"```", "", corrected_query).strip()

        if owner_table is not None:
            target_pipelines = [self._pipelines[owner_table]] if owner_table in self._pipelines else []
        else:
            target_pipelines = list(self._pipelines.values())

        for pipeline in target_pipelines:
            try:
                pipeline.apply_rectified_cte(corrected_query)
            except Exception as e:
                print(f"  [warn] apply_rectified_cte failed for {pipeline.source_table}: {e}")

        return corrected_query