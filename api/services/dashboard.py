import json

from core import db, json_utils
from core.ai_trace import log_ai_event
from core.sql_safety import classify_sql_error, is_safe_select, sanitize_sql
from services import llm_client
from services.query_generation import ORDER_BY_RULES

MAX_DASH_ROWS = 1000

def extract_dashboard_intent(
    table_id: int, user_prompt: str, knowledge_base: str, metadata: str, table_name: str
) -> dict:
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
      "sql_query": "TBD"
    }}
  ],

  "charts": [
    {{
      "id": "chart_1",
      "chart_type": "bar | column | line | pie | scatter",
      "title": "chart title",
      "description": "what insight this chart reveals",
      "x_column": "column name or null",
      "y_column": "column name or null",
      "color_column": null,
      "size_column": null,
      "aggregation": "sum | avg | count | none",
      "group_by": "column or null",
      "top_n": 10,
      "sort_by": "column or null",
      "sort_order": "asc | desc",
      "filters": [],
      "sql_query": "TBD",
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
1. Generate 4 to 6 clarification_questions across categories. Mark high-impact ones clearly.
2. For every KPI and chart set sql_query to exactly "TBD" — SQL is generated in a separate step.
3. Chart x_column / y_column / color_column / size_column must be the exact output names the final SQL will alias to.
4. Only use column names from AVAILABLE COLUMNS.
5. Return ONLY valid JSON."""
    raw = llm_client.ask_llm(system, user)
    print(f"LLM Response for Dashboard Intent: {raw}")
    return json_utils.extract_json_object(raw)

def _needs_sql(item: dict) -> bool:
    s = (item.get("sql_query") or "").strip()
    return not s or s.upper() == "TBD"

def _columns_info(meta: dict) -> dict:
    return {
        c: {
            "dtype": meta[c]["dtype"],
            "samples": meta[c].get("sample_values", meta[c].get("all_possible_values", [])),
        }
        for c in meta
    }

def _expected_columns(kind: str, item: dict) -> list:
    if kind == "kpi":
        return ["value"]
    return [item[k] for k in ("x_column", "y_column", "color_column", "size_column") if item.get(k)]

def generate_dashboard_sql(intent: dict, table_name: str, metadata, knowledge_base, trace_id: str, only_ids=None) -> dict:
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata
    todo = []
    for kind, key in (("kpi", "kpis"), ("chart", "charts")):
        for it in intent.get(key, []):
            if _needs_sql(it) and (only_ids is None or it["id"] in only_ids):
                todo.append({
                    "id": it["id"],
                    "kind": kind,
                    "spec": {k: v for k, v in it.items() if k not in ("sql_query", "status", "error")},
                    "must_output_columns": _expected_columns(kind, it),
                })
    if not todo:
        return {}

    system = f"""You write SQL Server SELECT queries for dashboard widgets. Return ONLY raw JSON:
{{"queries": [{{"id": "<item id>", "sql": "<SQL>"}}]}}  — one entry per requested item.

Rules:
1. Query only [{table_name}] (always bracketed). Use only columns listed in AVAILABLE COLUMNS.
2. Only SELECT. No semicolons, no INSERT/UPDATE/DELETE/DROP/ALTER/EXEC/INTO. No WITH/CTEs.
3. KPI: exactly one row, one column, aliased [value] (apply the aggregation to metric_column).
4. Chart: output columns must be aliased EXACTLY as must_output_columns, in that order.
   Apply the spec's aggregation and group_by; every non-aggregated column goes in GROUP BY.
5. Any division must use NULLIF(denominator, 0). Use exact value spellings from the samples when filtering.
6. top_n: use SELECT TOP (n) ... plus ORDER BY on the sort column. Without top_n add SELECT TOP {MAX_DASH_ROWS}.
7. Aliases with spaces use [brackets], never single quotes.

{ORDER_BY_RULES}
(Dashboard queries run standalone, so with TOP an ORDER BY is allowed; without TOP, end with OFFSET 0 ROWS.)"""
    user = (
        f"AVAILABLE COLUMNS:\n{json.dumps(_columns_info(meta), indent=2)}\n\n"
        f"DATASET CONTEXT:\n{knowledge_base}\n\nITEMS:\n{json.dumps(todo, indent=2)}"
    )
    raw = llm_client.ask_llm(system, user)
    log_ai_event(trace_id, "dashboard.sql_generated", raw_response=raw, item_ids=[t["id"] for t in todo])
    parsed = json_utils.extract_json_object(raw)
    return {q["id"]: q["sql"] for q in parsed.get("queries", []) if q.get("id") and q.get("sql")}

def _repair_dashboard_sql(item_id, kind, item, bad_sql, error, table_name, meta, trace_id) -> str:
    system = (
        "You fix a SQL Server dashboard query. Return ONLY raw JSON {\"sql\": \"...\"}. "
        f"Query only [{table_name}]; SELECT only; no WITH/CTEs; output columns aliased exactly "
        f"{_expected_columns(kind, item)} in that order; no semicolon.\n\n{ORDER_BY_RULES}"
    )
    user = (
        f"AVAILABLE COLUMNS:\n{json.dumps(_columns_info(meta), indent=2)}\n\n"
        f"ITEM:\n{json.dumps({k: v for k, v in item.items() if k != 'sql_query'}, indent=2)}\n\n"
        f"FAILED SQL:\n{bad_sql}\n\nERROR:\n{error}"
    )
    raw = llm_client.ask_llm(system, user)
    log_ai_event(trace_id, "dashboard.sql_repaired", item_id=item_id, raw_response=raw)
    return json_utils.extract_json_object(raw)["sql"]

def _run_item(kind: str, item: dict, sql: str) -> dict:
    ok, why = is_safe_select(sql)
    if not ok:
        raise PermissionError(f"Unsafe SQL rejected: {why}")

    res = db.fetch_with_columns(sql)
    cols, rows = list(res["columns"]), res["rows"][:MAX_DASH_ROWS]

    expected = _expected_columns(kind, item)
    lower = {c.lower(): c for c in cols}
    missing = [e for e in expected if e not in cols and e.lower() not in lower]
    if missing:
        raise ValueError(f"Result columns {cols} are missing expected columns {missing}")

    rename = {lower[e.lower()]: e for e in expected if e not in cols}
    cols = [rename.get(c, c) for c in cols]
    return {"columns": cols, "rows": [list(r) for r in rows]}

def build_dashboard(intent: dict, table_name: str, metadata, knowledge_base, trace_id: str, only_ids=None, allow_ai: bool = True):
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata

    for kind, key in (("kpi", "kpis"), ("chart", "charts")):
        for idx, item in enumerate(intent.get(key, []), 1):
            item.setdefault("id", f"{kind}_{idx}")

    generated = {}
    if allow_ai:
        try:
            generated = generate_dashboard_sql(intent, table_name, metadata, knowledge_base, trace_id, only_ids)
        except Exception as exc:
            log_ai_event(trace_id, "dashboard.sql_generation_failed", level="error", error=str(exc))

    data = {}
    for kind, key in (("kpi", "kpis"), ("chart", "charts")):
        for item in intent.get(key, []):
            iid = item["id"]
            if only_ids is not None and iid not in only_ids:
                continue
            if _needs_sql(item):
                item["sql_query"] = generated.get(iid, "")

            sql = sanitize_sql(item["sql_query"]) if item.get("sql_query") else ""
            if not sql:
                item["status"], item["error"] = "failed", "SQL_NOT_GENERATED"
                log_ai_event(trace_id, "dashboard.item_failed", level="error",
                             item_id=iid, error_code="SQL_NOT_GENERATED", item=item)
                continue

            for attempt in range(2):
                try:
                    data[iid] = _run_item(kind, item, sql)
                    item["sql_query"], item["status"], item["error"] = sql, "ok", None
                    break
                except Exception as exc:
                    info = classify_sql_error(exc)
                    log_ai_event(trace_id, "dashboard.item_execute_failed", level="error",
                                 item_id=iid, attempt=attempt, error_code=info["code"],
                                 error=str(exc), sql=sql, item=item)

                    unrepairable = isinstance(exc, PermissionError) or not allow_ai
                    if attempt == 1 or unrepairable:
                        item["sql_query"], item["status"], item["error"] = sql, "failed", info["code"]
                        break
                    try:
                        sql = sanitize_sql(
                            _repair_dashboard_sql(iid, kind, item, sql, str(exc), table_name, meta, trace_id)
                        )
                    except Exception as rex:
                        log_ai_event(trace_id, "dashboard.repair_failed", level="error",
                                     item_id=iid, error=str(rex))
                        item["sql_query"], item["status"], item["error"] = sql, "failed", info["code"]
                        break
    return intent, data

def _fmt_kpi(k: dict) -> str:
    return (
        f"  Title       : {k.get('title', '-')}\n"
        f"  Column      : {k.get('metric_column', '-')}\n"
        f"  Aggregation : {k.get('aggregation', '-')}\n"
        f"  Format      : {k.get('format', '-')} | "
        f"Prefix: '{k.get('prefix', '')}' | Suffix: '{k.get('suffix', '')}'\n"
        f"  Status      : {k.get('status', '-')}"
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
        f"  Description : {c.get('description', '-')}\n"
        f"  Status      : {c.get('status', '-')}"
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

def collect_feedback(response: str) -> str:
    return response

def apply_dashboard_feedback(dashboard_intent: dict, feedback: str, metadata: str) -> dict:
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata
    col_types = {col: meta[col]["dtype"] for col in meta}

    system = (
        "You are a senior BI dashboard designer. "
        "Update the dashboard specification according to the user's requested changes. "
        "Rules: preserve all existing fields unless changed; remove items explicitly deleted; "
        "add items explicitly requested; keep IDs unchanged where possible. "
        "Whenever you change a KPI/chart's column, aggregation, filters, grouping, top_n or sort, "
        "or add a new KPI/chart, set its sql_query to exactly \"TBD\" (SQL is regenerated later); "
        "leave sql_query untouched for items you did not change. "
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

DATA_KEYS = (
    "metric_column", "aggregation", "x_column", "y_column", "color_column",
    "size_column", "group_by", "top_n", "sort_by", "sort_order", "filters",
)

def find_item(intent: dict, item_id: str):
    for kind, key in (("kpi", "kpis"), ("chart", "charts")):
        for idx, it in enumerate(intent.get(key, [])):
            if it.get("id") == item_id:
                return kind, key, idx, it
    return None

def edit_dashboard_item(intent: dict, item_id: str, prompt: str, metadata, trace_id: str) -> dict:
    found = find_item(intent, item_id)
    if not found:
        raise KeyError(item_id)
    kind, key, idx, old = found
    meta = json.loads(metadata) if isinstance(metadata, str) else metadata

    system = (
        "You are a senior BI dashboard designer. Apply the user's request to exactly ONE dashboard widget. "
        "Return ONLY raw JSON: {\"action\": \"update\", \"item\": {<full widget JSON, same keys as input>}} "
        "or {\"action\": \"remove\"} if the user asks to delete it. "
        "Keep the id unchanged. Only use columns from AVAILABLE COLUMNS. "
        "For a chart, x_column/y_column/color_column/size_column must be the exact output names the SQL will alias to. "
        "Set sql_query to \"TBD\"."
    )
    user = (
        f"AVAILABLE COLUMNS:\n{json.dumps({c: meta[c]['dtype'] for c in meta}, indent=2)}\n\n"
        f"CURRENT WIDGET ({kind}):\n"
        f"{json.dumps({k: v for k, v in old.items() if k not in ('status', 'error')}, indent=2)}\n\n"
        f"USER REQUEST: {prompt}"
    )
    raw = llm_client.ask_llm(system, user)
    log_ai_event(trace_id, "dashboard.item_edit_generated", item_id=item_id, prompt=prompt, raw_response=raw)
    parsed = json_utils.extract_json_object(raw)

    if parsed.get("action") == "remove":
        intent[key].pop(idx)
        return {"action": "removed", "kind": kind, "key": key}

    new = parsed["item"]
    new["id"] = item_id
    if old.get("layout_position") is not None:
        new.setdefault("layout_position", old["layout_position"])
    changed = any(new.get(k) != old.get(k) for k in DATA_KEYS)
    new["sql_query"] = "TBD" if changed else (old.get("sql_query") or "TBD")
    new.pop("status", None)
    new.pop("error", None)
    intent[key][idx] = new
    return {"action": "updated", "kind": kind, "key": key}

KPI_PUBLIC_KEYS = ("id", "title", "format", "prefix", "suffix")
CHART_PUBLIC_KEYS = (
    "id", "title", "description", "chart_type", "x_column", "y_column", "aggregation", "sort_order",
)

def public_snapshot(spec: dict, data: dict) -> dict:
    def pick(item, keys):
        out = {k: item.get(k) for k in keys}
        out["status"] = "ok"
        return out

    kpis = [pick(k, KPI_PUBLIC_KEYS) for k in spec.get("kpis", []) if k.get("status") == "ok" and k["id"] in data]
    charts = [pick(c, CHART_PUBLIC_KEYS) for c in spec.get("charts", []) if c.get("status") == "ok" and c["id"] in data]
    keep = {i["id"] for i in kpis} | {i["id"] for i in charts}
    return {
        "spec": {
            "dashboard_title": spec.get("dashboard_title"),
            "dashboard_goal": spec.get("dashboard_goal"),
            "dashboard_type": spec.get("dashboard_type"),
            "target_audience": spec.get("target_audience"),
            "kpis": kpis,
            "charts": charts,
        },
        "data": {k: v for k, v in data.items() if k in keep},
    }