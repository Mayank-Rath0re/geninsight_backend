# services/ingestion.py
"""
Everything that happens to a dataset at upload time, before it becomes a
queryable table: loading the raw file, computing metadata/statistics,
classifying column roles for the SQL planner, building the LLM-authored
knowledge base, and the optional basic auto-cleaning transform.

(Was split across helper.get_dataframe and most of transform_handler.py.)
"""

import json
from typing import Any, Dict

import pandas as pd

from core import json_utils
from services import llm_client

UNIQUE_VALUES_THRESHOLD = 20


def get_dataframe(filepath: str) -> pd.DataFrame | None:
    ext = filepath.split("/")[-1].split(".")[-1]
    if ext == "csv":
        return pd.read_csv(filepath)
    if ext in ["xlsx", "xls", "xlsm", "xlsb", "ods"]:
        return pd.read_excel(filepath)
    return None


def extract_metadata(filepath: str) -> str:
    dataframe = get_dataframe(filepath)
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

        if pd.api.types.is_numeric_dtype(dataframe[col]):
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
                roles[col] = {
                    "role": "dimension",
                    "advice": (
                        f"Categorical dimension with {unique_ct} unique values. "
                        f"Ideal for GROUP BY. Sample: {meta['sample_values'][:3]}"
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


def build_knowledge_base(
    df: pd.DataFrame,
    metadata: str,
    table_name: str,
    roles: str,
) -> str:
    describe_str = df.describe(include="all").to_string()
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

    raw = llm_client.ask_llm(system, user)

    kb_dict = json_utils.extract_json_object(raw)
    kb_dict["column_roles"] = json.loads(roles)

    return json.dumps(kb_dict)


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
    print(f"Duplicates removed: {before - len(df)}")

    null_pct = df.isnull().mean()
    drop_cols = null_pct[null_pct > 0.8].index.tolist()
    if drop_cols:
        df.drop(columns=drop_cols, inplace=True)
    print(f"High-null columns dropped: {drop_cols or 'none'}")

    num_cols = df.select_dtypes(include="number").columns.tolist()
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


def sanitize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Shared column-name sanitization used at upload time, before any
    other processing (extracted from app.py's inline block so /upload
    and basic_transform can't drift apart on the sanitization regex)."""
    df = df.copy()
    df.columns = (
        df.columns
        .str.strip()
        .str.replace(r"[^A-Za-z0-9]+", "_", regex=True)
        .str.strip("_")
    )
    return df
