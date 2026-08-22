# services/dashboard.py
"""Dashboard intent extraction, rendering, and feedback (was dashboard_handler.py)."""

import json

from core import json_utils
from services import llm_client


def extract_dashboard_intent(
    table_id: int, user_prompt: str, knowledge_base: str, metadata: str, table_name: str
) -> dict:
    """
    Deep-parse the user prompt into a full dashboard specification.
    Returns a rich spec including charts, KPIs, layout, color preferences,
    and batched clarification questions (all in one call).
    """
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata

    columns_info = json.dumps({
        col: {
            "dtype": meta[col]["dtype"],
            "samples": meta[col].get("sample_values", meta[col].get("all_possible_values", [])),
        }
        for col in meta
    }, indent=2)

    system = (
        "You are a senior BI dashboard designer. Extract a complete dashboard specification "
        "from the user prompt and dataset context. Infer sensible defaults where possible. "
        "Return ONLY valid JSON — no markdown fences, no extra text."
    )
    user = f"""USER PROMPT: {user_prompt}
AVAILABLE COLUMNS:
{columns_info}
DATASET CONTEXT:
{knowledge_base}
Return a JSON with EXACTLY these keys:
{{
  "dashboard_title": "dashboard title",
  "dashboard_goal": "business objective",
  "dashboard_type": "executive | operational | analytical",
  "target_audience": "who will view this",
  "color_scheme": "inferred or default — e.g. blues, corporate, warm, etc.",
  "theme": "light | dark",

  "kpis": [
    {{
      "id": "kpi_1",
      "title": "KPI display name",
      "metric_column": "column name",
      "aggregation": "sum | avg | count | max | min",
      "format": "number | currency | percentage",
      "prefix": "$ or empty",
      "suffix": "% or M or empty",
      "sql_query": "SELECT ... FROM {table_name}"
    }}
  ],

  "charts": [
    {{
      "id": "chart_1",
      "chart_type": "bar | line (single line or multi-line) | pie | column | scatter | gauge | heatmap ",
      "title": "chart title",
      "description": "what insight this chart reveals",
      "x_column": "column name or null",
      "y_column": "column name or null",
      "color_column": "column for color grouping or null",
      "size_column": "column for bubble size or null",
      "aggregation": "sum | avg | count | none",
      "group_by": "column or null",
      "top_n": 10,
      "sort_by": "column or null",
      "sort_order": "asc | desc",
      "filters": [],
      "sql_query": "SELECT ... FROM {table_name} ... (to be generated next)",
      "layout_position": {{"row": 1, "col": 1, "width": 6, "height": 4}}
    }}
  ],
    "layout": {{
    "num_columns": 12,
    "kpi_row": true,
    "kpi_count": 2,
    "rows_of_charts": 2
  }},

  "clarification_questions": [
    {{
      "category": "data | visual | layout | style | audience",
      "question": "question text",
      "default_assumption": "what default will be used if user skips",
      "impact": "high | medium | low"
    }}
  ],
  "missing_info_notes": "any notes on info that would improve the dashboard"
}}

Rules:
1. Replace {{table}} placeholder in sql_query with actual table name: {table_name}
2. Generate 4 to 6 clarification_questions across categories. Mark high-impact ones clearly.
4. For each chart, set sql_query = 'TBD' — it will be generated in the SQL step.
5. Return ONLY valid JSON.
6. The y_column name in chart and result_df.column name after applying sql aliases should not be different, as they can cause error. If alias is used in sql, rename the columns to y column name after result_df generation also. """
    raw = llm_client.ask_llm(system, user)
    return json_utils.extract_json_array(raw)


# ============================================================================
# RENDERING
# ============================================================================

def _fmt_kpi(k: dict) -> str:
    return (
        f"  Title       : {k.get('title', '-')}\n"
        f"  Column      : {k.get('metric_column', '-')}\n"
        f"  Aggregation : {k.get('aggregation', '-')}\n"
        f"  Format      : {k.get('format', '-')} | "
        f"Prefix: '{k.get('prefix', '')}' | Suffix: '{k.get('suffix', '')}'"
    )


def _fmt_chart(c: dict) -> str:
    return (
        f"  Title       : {c.get('title', '-')}\n"
        f"  Type        : {c.get('chart_type', '-')}\n"
        f"  • X-axis:       {c.get('x_column', '—')}\n"
        f"  • Y-axis:       {c.get('y_column', '—')}\n"
        f"  Aggregation : {c.get('aggregation', '-')} | "
        f"Group by: {c.get('group_by', '-')}\n"
        f"  Color by    : {c.get('color_column', '-')} | "
        f"Top N: {c.get('top_n', '-')}\n"
        f"  Sort        : {c.get('sort_by', '-')} {c.get('sort_order', '')}\n"
        f"  Description : {c.get('description', '-')}"
    )


def render_dashboard_summary(intent: dict) -> str:
    lines = [
        "=" * 60,
        "DASHBOARD REVIEW",
        "=" * 60,
        f"Title        : {intent.get('dashboard_title', '-')}",
        f"Theme        : {intent.get('theme', '-')}",
        f"Color Scheme : {intent.get('color_scheme', '-')}",
        "",
        f"── KPIs ({len(intent.get('kpis', []))}) " + "─" * 40,
    ]
    for idx, kpi in enumerate(intent.get("kpis", []), 1):
        lines.append(f"\n[KPI {idx}]")
        lines.append(_fmt_kpi(kpi))

    lines += ["", f"── Charts ({len(intent.get('charts', []))}) " + "─" * 38]
    for idx, chart in enumerate(intent.get("charts", []), 1):
        lines.append(f"\n[Chart {idx}]")
        lines.append(_fmt_chart(chart))

    return "\n".join(lines)


# ============================================================================
# FEEDBACK
# ============================================================================

def collect_feedback(response: str) -> str:
    """
    Examples of feedback text:
      - Change KPI 2 aggregation to AVG
      - Make Chart 1 a line chart
      - Remove KPI 3
      - Add a Revenue by Region bar chart
      - Change theme to dark, use green palette

    An empty string means "accept and proceed" — passthrough for now,
    kept as its own function as the seam where feedback preprocessing
    (e.g. trimming, profanity filtering) would go.
    """
    return response


def apply_dashboard_feedback(dashboard_intent: dict, feedback: str, metadata: str) -> dict:
    """One LLM call that applies ALL user changes at once."""
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata
    col_types = {col: meta[col]["dtype"] for col in meta}

    system = (
        "You are a senior BI dashboard designer. "
        "Update the dashboard specification according to the user's requested changes. "
        "Rules: preserve all existing fields unless changed; remove items explicitly deleted; "
        "add items explicitly requested; keep IDs unchanged where possible. "
        "Return ONLY valid JSON — no markdown, no extra text."
    )
    user = (
        f"CURRENT DASHBOARD:\n{json.dumps(dashboard_intent, indent=2)}\n\n"
        f"AVAILABLE COLUMNS:\n{json.dumps(col_types, indent=2)}\n\n"
        f"USER REQUESTED CHANGES:\n{feedback}\n\n"
        "Return the FULL updated dashboard JSON."
    )
    raw = llm_client.ask_llm(system, user)
    try:
        updated = json_utils.extract_json_object(raw)
        updated["clarification_questions"] = []
        return updated
    except Exception:
        print("\n  Failed to parse LLM response. Keeping original dashboard.")
        return dashboard_intent
