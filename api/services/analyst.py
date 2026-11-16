# services/analyst.py
"""
AI Analyst: a 5-step trend analysis of one uploaded table.

Pipeline:
  1. design_analysis()  — one LLM call proposes the title, KPIs and five
     chart-backed analysis steps, constrained to the table's real columns.
  2. dashboard.build_dashboard() — the existing dashboard engine writes,
     validates, executes and (if needed) repairs the SQL for every widget.
  3. build_narrative() — one LLM call turns the *actual* result rows into an
     executive narrative plus a one-line takeaway per step. If that call
     fails, a deterministic summary is built from the same numbers.

Nothing is persisted here: the router stores the returned report in
`analyst_reports` so it can be reopened or shared without re-running the AI.
"""

import json
import logging

from core import json_utils
from core.ai_trace import log_ai_event
from services import dashboard as dashboard_service
from services import llm_client

logger = logging.getLogger("my_global_app_logger")

STEP_COUNT = 5
MAX_KPIS = 4
MAX_ROWS_IN_PROMPT = 8

CHART_TYPES = {"line", "bar", "column", "pie", "scatter"}
AGGREGATIONS = {"sum", "avg", "count", "max", "min"}


# ─────────────────────────────────────────────────────────────────────────
# Column helpers
# ─────────────────────────────────────────────────────────────────────────

def _metadata_dict(metadata) -> dict:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except ValueError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _dtype(meta: dict, column: str) -> str:
    return str((meta.get(column) or {}).get("dtype", "")).lower()


def _is_numeric(meta: dict, column: str) -> bool:
    dt = _dtype(meta, column)
    if any(token in dt for token in ("int", "float", "double", "decimal", "numeric", "real", "number")):
        return True
    if "date" in dt or "time" in dt or "bool" in dt:
        return False
    return isinstance((meta.get(column) or {}).get("mean"), (int, float))


def _is_date(meta: dict, column: str) -> bool:
    return "date" in _dtype(meta, column) or "time" in _dtype(meta, column)


def _numeric_columns(meta: dict) -> list:
    return [c for c in meta if _is_numeric(meta, c)]


def _date_columns(meta: dict) -> list:
    return [c for c in meta if _is_date(meta, c)]


def _categorical_columns(meta: dict) -> list:
    return [c for c in meta if c not in _numeric_columns(meta) and c not in _date_columns(meta)]


def _columns_info(meta: dict) -> dict:
    out = {}
    for col, info in meta.items():
        info = info or {}
        samples = info.get("sample_values", info.get("all_possible_values", []))
        out[col] = {"dtype": info.get("dtype"), "samples": samples[:8]}
    return out


def _looks_like_id(column: str) -> bool:
    col = column.lower()
    return col == "id" or col.endswith(("_id", "_no", "_code", "_key", "_num", "uuid"))


def _kpi_candidates(meta: dict) -> list:
    """Numeric measures that make sense to sum/average — excludes id-like
    columns and low-cardinality numerics (years, flags, codes)."""
    numeric = _numeric_columns(meta)
    good = [
        c for c in numeric
        if not _looks_like_id(c) and int((meta.get(c) or {}).get("unique_count") or 0) > 20
    ]
    return good or [c for c in numeric if not _looks_like_id(c)] or numeric


# ─────────────────────────────────────────────────────────────────────────
# Step 1 — design the analysis
# ─────────────────────────────────────────────────────────────────────────

def _fallback_steps(meta: dict) -> list:
    """Deterministic five-step plan built only from real column metadata.

    Used when the LLM is unavailable or returns something unusable, and to
    fill any gaps in a partially valid LLM plan.
    """
    cols = list(meta.keys())
    numeric = _numeric_columns(meta)
    dates = _date_columns(meta)
    cats = _categorical_columns(meta)

    measure = numeric[0] if numeric else (cols[0] if cols else None)
    measure2 = numeric[1] if len(numeric) > 1 else measure
    time_col = dates[0] if dates else (cats[0] if cats else None)
    cat1 = cats[0] if cats else time_col
    cat2 = cats[1] if len(cats) > 1 else cat1
    agg = "sum" if numeric else "count"

    steps = []

    def add(title, question, chart_type, x, y, aggregation, order, top_n=10):
        x = x or measure
        y = y or measure
        if not x or not y:
            return
        steps.append({
            "id": f"step_{len(steps) + 1}",
            "title": title,
            "question": question,
            "description": question,
            "chart_type": chart_type,
            "x_column": x,
            "y_column": y,
            "color_column": None,
            "aggregation": aggregation,
            "group_by": x,
            "top_n": top_n,
            "sort_order": order,
            "sql_query": "TBD",
        })

    add(
        f"Overall trend of {_label(measure)}",
        f"How does {_label(measure)} move over {_label(time_col)}?",
        "line", time_col, measure, agg, "asc", 50,
    )
    add(
        f"Top {_label(cat1)} by {_label(measure)}",
        f"Which {_label(cat1)} contribute the most {_label(measure)}?",
        "bar", cat1, measure, agg, "desc", 10,
    )
    add(
        f"Mix by {_label(cat2)}",
        f"How is {_label(measure)} distributed across {_label(cat2)}?",
        "pie", cat2, measure, agg, "desc", 8,
    )
    add(
        f"{_label(cat2)} comparison",
        f"How does {_label(measure2)} compare between {_label(cat2)} values?",
        "column", cat2, measure2, agg, "desc", 10,
    )
    add(
        f"{_label(measure2)} outliers",
        f"Where does {_label(measure2)} deviate from the overall pattern?",
        "scatter", cat1, measure2, agg, "desc", 20,
    )
    return steps


def _label(column) -> str:
    if not column:
        return "the data"
    return str(column).replace("_", " ").strip() or "the data"


def _fallback_plan(meta: dict, table_name: str) -> dict:
    kpis = []
    for idx, col in enumerate(_kpi_candidates(meta)[:3], 1):
        kpis.append({
            "id": f"kpi_{idx}",
            "title": _label(col).title(),
            "metric_column": col,
            "aggregation": "sum",
            "format": "number",
            "prefix": "",
            "suffix": "",
            "sql_query": "TBD",
        })
    return {
        "analysis_title": f"{_label(table_name).title()} — Trend Analysis",
        "analysis_goal": "Explain the overall trend and its main drivers.",
        "target_audience": "Business stakeholders",
        "kpis": kpis,
        "steps": _fallback_steps(meta),
    }


def _normalise_kpis(raw, meta: dict) -> list:
    if not isinstance(raw, list):
        return []
    columns = list(meta.keys())
    numeric = _numeric_columns(meta)
    out = []
    for item in raw[:MAX_KPIS]:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric_column")
        if metric not in meta:
            candidates = _kpi_candidates(meta)
            metric = candidates[0] if candidates else (columns[0] if columns else None)
        if not metric:
            continue
        agg = str(item.get("aggregation") or "sum").lower()
        if agg not in AGGREGATIONS:
            agg = "sum"
        if not numeric and agg != "count":
            agg = "count"
        fmt = str(item.get("format") or "number").lower()
        if fmt not in ("number", "currency", "percentage"):
            fmt = "number"
        out.append({
            "id": f"kpi_{len(out) + 1}",
            "title": str(item.get("title") or _label(metric).title())[:80],
            "metric_column": metric,
            "aggregation": agg,
            "format": fmt,
            "prefix": str(item.get("prefix") or "")[:8],
            "suffix": str(item.get("suffix") or "")[:8],
            "sql_query": "TBD",
        })
    return out


def _normalise_steps(raw, meta: dict, fallback_steps: list) -> list:
    fallback_iter = (fallback_steps * STEP_COUNT)[:STEP_COUNT]
    steps = []
    provided = raw if isinstance(raw, list) else []

    for idx in range(STEP_COUNT):
        base = fallback_iter[idx] if idx < len(fallback_iter) else None
        item = provided[idx] if idx < len(provided) and isinstance(provided[idx], dict) else None
        if item is None and base is None:
            continue

        item = item or {}
        x = item.get("x_column")
        y = item.get("y_column")
        if x not in meta:
            x = (base or {}).get("x_column")
        if y not in meta:
            y = (base or {}).get("y_column")
        if not x or not y:
            continue

        chart_type = str(item.get("chart_type") or (base or {}).get("chart_type") or "bar").lower()
        if chart_type not in CHART_TYPES:
            chart_type = "bar"

        agg = str(item.get("aggregation") or (base or {}).get("aggregation") or "sum").lower()
        if agg not in AGGREGATIONS:
            agg = "sum"
        if not _numeric_columns(meta) and agg != "count":
            agg = "count"

        group_by = item.get("group_by")
        if group_by not in meta:
            group_by = x

        try:
            top_n = int(item.get("top_n") or (base or {}).get("top_n") or 10)
        except (TypeError, ValueError):
            top_n = 10
        top_n = max(1, min(top_n, 50))

        sort_order = str(item.get("sort_order") or (base or {}).get("sort_order") or "desc").lower()
        if sort_order not in ("asc", "desc"):
            sort_order = "desc"

        title = str(item.get("title") or (base or {}).get("title") or f"Analysis step {idx + 1}")[:120]
        question = str(item.get("question") or item.get("description") or (base or {}).get("question") or "")[:300]

        steps.append({
            "id": f"step_{idx + 1}",
            "title": title,
            "question": question,
            "description": question,
            "chart_type": chart_type,
            "x_column": x,
            "y_column": y,
            "color_column": None,
            "aggregation": agg,
            "group_by": group_by,
            "top_n": top_n,
            "sort_order": sort_order,
            "sql_query": "TBD",
        })
    return steps


def _normalise_plan(plan, meta: dict, table_name: str) -> dict:
    fallback = _fallback_plan(meta, table_name)
    if not isinstance(plan, dict):
        return fallback

    kpis = _normalise_kpis(plan.get("kpis"), meta)
    steps = _normalise_steps(plan.get("steps"), meta, fallback["steps"])
    if len(steps) < STEP_COUNT:
        for base in fallback["steps"]:
            if len(steps) >= STEP_COUNT:
                break
            steps.append({**base, "id": f"step_{len(steps) + 1}"})

    return {
        "analysis_title": str(plan.get("analysis_title") or fallback["analysis_title"])[:160],
        "analysis_goal": str(plan.get("analysis_goal") or fallback["analysis_goal"])[:400],
        "target_audience": str(plan.get("target_audience") or fallback["target_audience"])[:120],
        "kpis": kpis or fallback["kpis"],
        "steps": steps if steps else fallback["steps"],
    }


def design_analysis(table_name: str, metadata, knowledge_base, trace_id: str) -> dict:
    meta = _metadata_dict(metadata)
    if not meta:
        raise ValueError("This table has no column metadata to analyse.")

    system = (
        "You are a senior data analyst. Design a five-step analysis that explains the trend "
        "in a dataset. Every step must be answerable with one SQL aggregate over the table. "
        "Use ONLY the columns in AVAILABLE COLUMNS — never invent column names. "
        "Return ONLY valid JSON, no markdown fences."
    )
    user = f"""TABLE: {table_name}

AVAILABLE COLUMNS:
{json.dumps(_columns_info(meta), indent=2)}

DATASET CONTEXT:
{knowledge_base}

Return JSON with EXACTLY these keys:
{{
  "analysis_title": "short title for the analysis",
  "analysis_goal": "what question this analysis answers",
  "target_audience": "who should read this",
  "kpis": [
    {{
      "title": "KPI display name",
      "metric_column": "exact column name",
      "aggregation": "sum | avg | count | max | min",
      "format": "number | currency | percentage",
      "prefix": "$ or empty",
      "suffix": "% or empty"
    }}
  ],
  "steps": [
    {{
      "title": "step title",
      "question": "the business question this step answers",
      "chart_type": "line | bar | column | pie | scatter",
      "x_column": "exact column name",
      "y_column": "exact column name",
      "aggregation": "sum | avg | count | max | min",
      "group_by": "exact column name",
      "top_n": 10,
      "sort_order": "asc | desc"
    }}
  ]
}}

Rules:
1. Return exactly 3 KPIs and exactly 5 steps, in this narrative order:
   overall trend over time -> what drives it -> top performers -> mix/composition
   -> comparison or outliers.
2. If a date/time column exists, step 1 must use it on the x-axis with
   sort_order "asc". Otherwise use the main categorical column.
3. Use count aggregation when there is no numeric measure; still set
   y_column to a real column.
4. Keep titles under 60 characters and questions under 140 characters.
5. Return ONLY valid JSON."""

    plan = None
    try:
        raw = llm_client.ask_llm(system, user)
        log_ai_event(trace_id, "analyst.plan_generated", raw_response=raw)
        plan = json_utils.extract_json_object(raw)
    except Exception as exc:
        log_ai_event(trace_id, "analyst.plan_failed", level="error", error=str(exc))

    return _normalise_plan(plan, meta, table_name)


# ─────────────────────────────────────────────────────────────────────────
# Step 2 — narrative from the actual result rows
# ─────────────────────────────────────────────────────────────────────────

def _fmt_number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    magnitude = abs(number)
    if magnitude >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if magnitude >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if magnitude >= 1_000:
        return f"{number / 1_000:.2f}K"
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _value_axis(spec: dict, columns: list) -> int:
    y = spec.get("y_column")
    if y in columns:
        return columns.index(y)
    return 1 if len(columns) > 1 else 0


def _step_rows(step: dict, data: dict) -> tuple:
    payload = data.get(step["id"])
    if not isinstance(payload, dict):
        return [], []
    return list(payload.get("columns") or []), list(payload.get("rows") or [])


def _digest(steps: list, kpis: list, data: dict) -> str:
    items = []
    for kpi in kpis:
        payload = data.get(kpi["id"])
        rows = (payload or {}).get("rows") or []
        value = rows[0][0] if rows and rows[0] else None
        items.append({
            "kind": "kpi",
            "title": kpi.get("title"),
            "aggregation": kpi.get("aggregation"),
            "column": kpi.get("metric_column"),
            "value": value,
        })
    for step in steps:
        columns, rows = _step_rows(step, data)
        items.append({
            "kind": "step",
            "id": step["id"],
            "title": step.get("title"),
            "question": step.get("question"),
            "chart_type": step.get("chart_type"),
            "x": step.get("x_column"),
            "y": step.get("y_column"),
            "aggregation": step.get("aggregation"),
            "status": step.get("status"),
            "columns": columns,
            "rows": rows[:MAX_ROWS_IN_PROMPT],
        })
    text = json.dumps(items, default=str)
    return text[:12000]


def _deterministic_summary(steps: list, kpis: list, data: dict) -> tuple:
    sentences = []
    insights = {}

    for kpi in kpis:
        rows = (data.get(kpi["id"]) or {}).get("rows") or []
        if rows and rows[0]:
            sentences.append(
                f"{kpi.get('title') or 'Overall total'} is {_fmt_number(rows[0][0])}."
            )

    for step in steps:
        columns, rows = _step_rows(step, data)
        if not rows:
            insights[step["id"]] = "This step couldn't be computed from the available data."
            continue
        yi = _value_axis(step, columns)
        xi = 0 if yi != 0 else min(1, len(columns) - 1)

        def key(row):
            return row[xi] if xi < len(row) else ""

        def value(row):
            raw = row[yi] if yi < len(row) else None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        valued = [(key(r), value(r)) for r in rows]
        valued = [(k, v) for k, v in valued if v is not None]
        if not valued:
            insights[step["id"]] = "The result contained no numeric values to summarise."
            continue

        if step.get("chart_type") == "line" and len(valued) > 1:
            first_key, first_value = valued[0]
            last_key, last_value = valued[-1]
            if first_value:
                change = (last_value - first_value) / abs(first_value) * 100
                direction = "up" if change >= 0 else "down"
                sentence = (
                    f"{step.get('title')}: from {first_key} to {last_key} the value moved "
                    f"{direction} {abs(change):.1f}% ({_fmt_number(first_value)} → "
                    f"{_fmt_number(last_value)})."
                )
            else:
                sentence = (
                    f"{step.get('title')}: it starts at {_fmt_number(first_value)} "
                    f"({first_key}) and ends at {_fmt_number(last_value)} ({last_key})."
                )
        else:
            leader_key, leader_value = max(valued, key=lambda kv: abs(kv[1]))
            total = sum(abs(v) for _, v in valued) or 1
            share = abs(leader_value) / total * 100
            sentence = (
                f"{step.get('title')}: {leader_key} leads with "
                f"{_fmt_number(leader_value)} ({share:.1f}% of the top results)."
            )
        sentences.append(sentence)
        insights[step["id"]] = sentence.split(":", 1)[-1].strip()

    if not sentences:
        sentences.append("There wasn't enough data to produce a reliable summary.")
    return " ".join(sentences[:8]), insights


def build_narrative(plan: dict, data: dict, trace_id: str) -> tuple:
    steps, kpis = plan["steps"], plan["kpis"]
    digest = _digest(steps, kpis, data)

    system = (
        "You are a senior data analyst writing an executive summary. "
        "Use ONLY the numbers and column names in ANALYSIS RESULTS. "
        "Never invent a number, trend or column. If a step failed, say that the step "
        "couldn't be calculated instead of guessing. Return ONLY valid JSON."
    )
    user = f"""ANALYSIS: {plan['analysis_title']}
GOAL: {plan['analysis_goal']}

ANALYSIS RESULTS (JSON):
{digest}

Return JSON:
{{
  "narrative_summary": "one or two short paragraphs (80-160 words) explaining the trend, its main drivers and what stands out",
  "step_insights": {{
    "step_1": "one sentence (max 30 words) with the key takeaway, quoting a real figure",
    "step_2": "...",
    "step_3": "...",
    "step_4": "...",
    "step_5": "..."
  }}
}}"""

    narrative = ""
    insights = {}
    try:
        raw = llm_client.ask_llm(system, user)
        log_ai_event(trace_id, "analyst.narrative_generated", raw_response=raw)
        parsed = json_utils.extract_json_object(raw)
        narrative = str(parsed.get("narrative_summary") or "").strip()
        raw_insights = parsed.get("step_insights")
        if isinstance(raw_insights, dict):
            insights = {str(k): str(v).strip() for k, v in raw_insights.items() if v}
    except Exception as exc:
        log_ai_event(trace_id, "analyst.narrative_failed", level="error", error=str(exc))

    fallback_text, fallback_insights = _deterministic_summary(steps, kpis, data)
    for step in steps:
        sid = step["id"]
        if not insights.get(sid):
            insights[sid] = fallback_insights.get(sid, "")
    return (narrative or fallback_text), insights


# ─────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────

def build_analysis(table_id: int, table_name: str, metadata, knowledge_base, trace_id: str) -> dict:
    plan = design_analysis(table_name, metadata, knowledge_base, trace_id)

    intent = {"kpis": plan["kpis"], "charts": plan["steps"]}
    spec, data = dashboard_service.build_dashboard(
        intent, table_id, table_name, metadata, knowledge_base, trace_id
    )

    ok_steps = sum(1 for step in plan["steps"] if step.get("status") == "ok")
    ok_kpis = sum(1 for kpi in plan["kpis"] if kpi.get("status") == "ok")
    if ok_steps == 0 and ok_kpis == 0:
        raise ValueError("No analysis step could be computed from this table.")

    narrative, insights = build_narrative(plan, data, trace_id)

    steps_payload = []
    for step in plan["steps"]:
        steps_payload.append({
            "id": step["id"],
            "title": step.get("title"),
            "question": step.get("question"),
            "chart_type": step.get("chart_type"),
            "x_column": step.get("x_column"),
            "y_column": step.get("y_column"),
            "aggregation": step.get("aggregation"),
            "status": step.get("status", "failed"),
            "error": step.get("error"),
            "insight": insights.get(step["id"], ""),
        })

    log_ai_event(
        trace_id,
        "analyst.done",
        steps_ok=ok_steps,
        steps_failed=STEP_COUNT - ok_steps,
        kpis_ok=ok_kpis,
    )

    return {
        "table_id": table_id,
        "table_name": table_name,
        "analysis_title": plan["analysis_title"],
        "analysis_goal": plan["analysis_goal"],
        "target_audience": plan["target_audience"],
        "narrative_summary": narrative,
        "steps": steps_payload,
        "spec": {
            "dashboard_title": plan["analysis_title"],
            "dashboard_goal": plan["analysis_goal"],
            "dashboard_type": "analytical",
            "target_audience": plan["target_audience"],
            "kpis": plan["kpis"],
            "charts": plan["steps"],
        },
        "data": data,
        "trace_id": trace_id,
    }


# ─────────────────────────────────────────────────────────────────────────
# Public snapshot (shared links — no SQL, only rendered results)
# ─────────────────────────────────────────────────────────────────────────

def public_snapshot(result: dict) -> dict:
    """Strip SQL and failed items from a stored report so a shared link only
    exposes what the owner sees on screen."""
    spec = result.get("spec") or {}
    data = result.get("data") or {}

    def pick(item, keys):
        out = {k: item.get(k) for k in keys}
        out["status"] = "ok"
        return out

    kpis = [
        pick(k, dashboard_service.KPI_PUBLIC_KEYS)
        for k in spec.get("kpis", [])
        if k.get("status") == "ok" and k.get("id") in data
    ]
    charts = [
        pick(c, dashboard_service.CHART_PUBLIC_KEYS)
        for c in spec.get("charts", [])
        if c.get("status") == "ok" and c.get("id") in data
    ]
    keep = {i.get("id") for i in kpis} | {i.get("id") for i in charts}

    steps = []
    for step in result.get("steps") or []:
        if step.get("id") not in keep:
            continue
        steps.append({
            "id": step.get("id"),
            "title": step.get("title"),
            "question": step.get("question"),
            "chart_type": step.get("chart_type"),
            "x_column": step.get("x_column"),
            "y_column": step.get("y_column"),
            "status": "ok",
            "insight": step.get("insight") or "",
        })

    return {
        "table_name": result.get("table_name"),
        "analysis_title": result.get("analysis_title"),
        "analysis_goal": result.get("analysis_goal"),
        "target_audience": result.get("target_audience"),
        "narrative_summary": result.get("narrative_summary") or "",
        "steps": steps,
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
