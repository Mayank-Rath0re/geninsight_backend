"""
Everything that happens to a dataset at upload time, before it becomes a
queryable table: loading the raw file, computing metadata/statistics,
classifying column roles for the SQL planner, building the LLM-authored
knowledge base (now including an AI-generated session title in the same
call), and the optional basic auto-cleaning transform.
"""

import json
import logging
from typing import Any, Dict

import pandas as pd

from core import json_utils
from services import llm_client

logger = logging.getLogger("my_global_app_logger")

UNIQUE_VALUES_THRESHOLD = 20


def get_dataframe(filepath: str) -> pd.DataFrame | None:
    ext = filepath.split("/")[-1].split(".")[-1]
    if ext == "csv":
        return pd.read_csv(filepath)
    if ext in ["xlsx", "xls", "xlsm", "xlsb", "ods"]:
        return pd.read_excel(filepath)
    return None


def extract_metadata(filepath: str) -> str:
    return metadata_from_dataframe(get_dataframe(filepath))


def metadata_from_dataframe(dataframe: pd.DataFrame) -> str:
    """Per-column statistics used by the SQL planner and dashboard generator.
    Shared by file uploads and custom-database table registration."""
    meta: Dict[str, Any] = {}
    for col in dataframe.columns:
        unique_count = int(dataframe[col].nunique())

        meta[col] = {
            "dtype": str(dataframe[col].dtype),
            "null_count": int(dataframe[col].isnull().sum()),
            "null_pct": round(dataframe[col].isnull().mean() * 100, 2),
            "unique_count": unique_count,
        }

        non_null_values = dataframe[col].dropna().unique()

        if unique_count <= UNIQUE_VALUES_THRESHOLD:
            meta[col]["all_possible_values"] = [str(v) for v in non_null_values.tolist()]
        else:
            meta[col]["sample_values"] = [str(v) for v in non_null_values[:5].tolist()]

        if pd.api.types.is_numeric_dtype(dataframe[col]) and unique_count:
            meta[col]["min"] = str(round(float(dataframe[col].min()), 4))
            meta[col]["max"] = str(round(float(dataframe[col].max()), 4))
            meta[col]["mean"] = str(round(float(dataframe[col].mean()), 4))

    return json.dumps(meta, default=str)


def classify_column_roles(df: pd.DataFrame, metadata: str) -> str:
    """
    Classify each column into an analytical role so the LLM knows how to
    use it in GROUP BY and aggregations.
    """
    roles = {}
    n_rows = len(df)

    metadata_dict = json.loads(metadata)
    for col, meta in metadata_dict.items():
        dtype = meta["dtype"]
        unique_ct = meta["unique_count"]
        cardinality = unique_ct / n_rows if n_rows > 0 else 0
        col_lower = col.lower()

        is_id_name = any(
            col_lower == pat or col_lower.endswith(pat)
            for pat in ("id", "_id", "no", "_no", "code", "key", "uuid", "num")
        )
        if is_id_name and cardinality > 0.5:
            roles[col] = {
                "role": "identifier",
                "advice": "Do NOT use in GROUP BY or aggregate. Omit from SELECT when aggregating.",
            }
            continue

        if (
            "date" in col_lower or "time" in col_lower or "dt" in col_lower
            or dtype.startswith("datetime")
        ):
            roles[col] = {
                "role": "date",
                "advice": "Use in GROUP BY for time-series. Can use DATE(), MONTH(), YEAR().",
            }
            continue

        if dtype in ("int64", "float64", "int32", "float32"):
            if unique_ct <= 20:
                roles[col] = {
                    "role": "dimension",
                    "advice": "Low-cardinality numeric — treat as GROUP BY dimension.",
                }
            else:
                roles[col] = {
                    "role": "metric",
                    "advice": (
                        f"Aggregate with SUM(), AVG(), MAX(), MIN(). "
                        f"Range: {meta.get('min')} to {meta.get('max')}. "
                        "Do NOT put in GROUP BY unless explicitly requested."
                    ),
                }
            continue

        if dtype == "object":
            if cardinality < 0.05:
                values = meta.get("all_possible_values") or meta.get("sample_values") or []
                roles[col] = {
                    "role": "dimension",
                    "advice": (
                        f"Categorical dimension with {unique_ct} unique values. "
                        f"Ideal for GROUP BY. Values: {values[:10]}"
                    ),
                }
            else:
                roles[col] = {
                    "role": "text",
                    "advice": "High-cardinality text. Avoid GROUP BY unless filtering to specific values.",
                }
            continue

        roles[col] = {"role": "unknown", "advice": "Use with caution."}
    return json.dumps(roles, indent=2, default=str)


def _humanize_table_name(table_name: str) -> str:
    """'user3_quarterly_sales_xlsx' -> 'Quarterly Sales Xlsx'."""
    parts = [p for p in table_name.split("_") if p]
    if parts and parts[0].startswith("user") and parts[0][4:].isdigit():
        parts = parts[1:]
    return " ".join(p.capitalize() for p in parts) or "Dataset"


def _default_session_title(table_name: str) -> str:
    return f"{_humanize_table_name(table_name)[:60]} Analysis"


def _column_families(metadata: str, columns: list) -> tuple[list, list, list]:
    meta = json.loads(metadata)
    numeric, dates = [], []
    for col in columns:
        dtype = str(meta.get(col, {}).get("dtype", ""))
        col_lower = col.lower()
        if dtype.startswith("datetime") or any(t in col_lower for t in ("date", "time", "dt")):
            dates.append(col)
        elif dtype.startswith(("int", "float")):
            numeric.append(col)
    categorical = [c for c in columns if c not in numeric and c not in dates]
    return numeric, dates, categorical


def _fallback_knowledge_base(
    df: pd.DataFrame, metadata: str, table_name: str, roles: str
) -> dict:
    """Deterministic knowledge base used when the LLM call or JSON parse fails,
    so an upload never hard-fails because a provider is down."""
    columns = list(df.columns)
    numeric, dates, categorical = _column_families(metadata, columns)
    kb_dict = {
        "columns_name": columns,
        "table_name": table_name,
        "domain": "unclassified",
        "description": (
            f"Dataset '{_humanize_table_name(table_name)}' with {df.shape[0]} rows "
            f"and {df.shape[1]} columns: {', '.join(map(str, columns[:10]))}."
        ),
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
        "key_columns": (numeric[:3] + categorical[:2]) or columns[:5],
        "date_columns": dates,
        "numeric_columns": numeric,
        "categorical_columns": categorical,
        "data_quality_notes": "Automatic profile — generated without AI.",
        "potential_analyses": [],
        "column_roles": json.loads(roles),
    }
    return {"knowledge_base": json.dumps(kb_dict), "session_title": _default_session_title(table_name)}


def _sanitize_kb(kb_dict: dict, df: pd.DataFrame, metadata: str, table_name: str, roles: str) -> dict:
    """Overwrite model-reported structure with ground truth from the dataframe so a
    hallucinated column, row count, or table name can never reach the SQL planner."""
    columns = list(df.columns)
    numeric, dates, categorical = _column_families(metadata, columns)
    kb_dict["columns_name"] = columns
    kb_dict["table_name"] = table_name
    kb_dict["row_count"] = int(df.shape[0])
    kb_dict["column_count"] = int(df.shape[1])
    kb_dict["numeric_columns"] = [c for c in kb_dict.get("numeric_columns") or [] if c in numeric] or numeric
    kb_dict["date_columns"] = [c for c in kb_dict.get("date_columns") or [] if c in dates] or dates
    kb_dict["categorical_columns"] = [
        c for c in kb_dict.get("categorical_columns") or [] if c in categorical
    ] or categorical
    kb_dict["key_columns"] = [c for c in kb_dict.get("key_columns") or [] if c in columns] or columns[:5]
    kb_dict["column_roles"] = json.loads(roles)
    return kb_dict


def build_knowledge_base(
    df: pd.DataFrame,
    metadata: str,
    table_name: str,
    roles: str,
) -> dict:
    """
    Single AI call that produces both the knowledge base AND a proposed
    session title (session_title), so staging a session never needs a
    second, dedicated "name this session" call.

    Pass the FULL dataframe: row_count/describe must reflect the whole
    dataset, not a sample. On LLM or JSON failure this returns a
    deterministic, metadata-derived knowledge base instead of raising.

    Returns a dict with keys: "knowledge_base" (str, JSON-encoded — same
    shape/contract as before) and "session_title" (str).
    """
    # Keep the prompt bounded: full row count goes in the schema block, while the
    # descriptive stats come from at most 2000 rows / 40 columns.
    stats_frame = df.iloc[:2000, :40] if len(df) > 2000 or df.shape[1] > 40 else df
    describe_str = stats_frame.describe(include="all").to_string()
    sample_str = df.head(5).to_string(index=False)
    cols = list(df.columns)

    system = (
        "You are a senior data analyst. "
        "Respond with ONLY a single raw JSON object — no markdown fences, "
        "no explanation, no preamble, no trailing text. "
        "The response must be directly parseable by json.loads()."
    )

    user = f"""
SCHEMA METADATA:
{metadata}

STATISTICAL SUMMARY (sampled; SCHEMA METADATA has full-data ranges):
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
  "potential_analyses": ["3-5 analysis ideas this data supports"],
  "session_title": "a short (3-6 word) human-friendly title for a new analysis session on this dataset, e.g. 'Q3 Sales Analysis' or 'Customer Churn Data'"
}}"""
    chars = len(system) + len(user)
    logger.info(
        "Calling AI for knowledge base + session title (%s chars ≈ %s tokens)",
        chars, chars // 4,
    )

    try:
        raw = llm_client.ask_llm(system, user)
        kb_dict = json_utils.extract_json_object(raw)
        if not isinstance(kb_dict, dict):
            raise ValueError("Knowledge base response was not a JSON object.")
    except Exception:
        logger.exception("Knowledge base generation failed for %s; using deterministic fallback", table_name)
        return _fallback_knowledge_base(df, metadata, table_name, roles)

    session_title = kb_dict.pop("session_title", None)
    if not isinstance(session_title, str) or not session_title.strip():
        session_title = _default_session_title(table_name)
    kb_dict = _sanitize_kb(kb_dict, df, metadata, table_name, roles)

    return {"knowledge_base": json.dumps(kb_dict), "session_title": session_title.strip()[:120]}


def basic_transform(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standard auto-transforms run before any user-requested transforms:
      1. Remove exact duplicate rows
      2. Drop columns where >80% values are null
      3. Fill numeric nulls with column median
      4. Fill categorical nulls with 'Unknown'
    """
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.replace(r"[^A-Za-z0-9]+", "_", regex=True)
        .str.strip("_")
    )

    before = len(df)
    df.drop_duplicates(inplace=True)
    logger.info("Basic transform: %s duplicate rows removed", before - len(df))

    null_pct = df.isnull().mean()
    drop_cols = null_pct[null_pct > 0.8].index.tolist()
    if drop_cols:
        df.drop(columns=drop_cols, inplace=True)
    logger.info("Basic transform: high-null columns dropped: %s", drop_cols or "none")

    num_cols = df.select_dtypes(include="number").columns.tolist()
    filled_num = []
    for col in num_cols:
        if df[col].isnull().sum() > 0:
            median_val = df[col].median()
            if pd.notnull(median_val):
                df[col] = df[col].fillna(median_val)
            filled_num.append(col)
    logger.info("Basic transform: numeric nulls filled: %s", filled_num or "none")

    try:
        cat_cols = df.select_dtypes(include=["string", "category"]).columns.tolist()
    except Exception:
        cat_cols = df.select_dtypes(include=["object", "category"]).columns.tolist()

    filled_cat = []
    for col in cat_cols:
        if df[col].isnull().sum() > 0:
            df[col] = df[col].fillna("Unknown")
            filled_cat.append(col)
    logger.info("Basic transform: categorical nulls filled: %s", filled_cat or "none")

    return df


def sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Shared column-name sanitization used at upload time, before any
    other processing.

    Beyond stripping punctuation (SQL identifiers), this guarantees every
    header is non-empty, unique (pandas de-duplicates differently per
    format), and within SQL Server's 128-character identifier limit — a
    duplicate or empty header used to make CREATE TABLE fail mid-upload.
    """
    df = df.copy()
    cleaned = (
        df.columns.astype(str)
        .str.strip()
        .str.replace(r"[^A-Za-z0-9]+", "_", regex=True)
        .str.strip("_")
    )
    seen: set[str] = set()
    names: list[str] = []
    for i, col in enumerate(cleaned):
        base = (col or f"column_{i + 1}")[:120]
        name = base
        suffix = 1
        while name in seen:
            name = f"{base[:116]}_{suffix}"
            suffix += 1
        seen.add(name)
        names.append(name)
    df.columns = names
    return df