# helper.py

import json 
import re 
import pandas as pd
from typing import List
import string

def get_dataframe(filepath: str) -> pd.DataFrame:
    ext = filepath.split("/")[-1].split(".")[-1]
    if ext == "csv":
        df = pd.read_csv(filepath)
    elif ext in ["xlsx", "xls", "xlsm", "xlsb", "ods"]:
        df = pd.read_excel(filepath)
    else:
        df = None
    return df  # ← this was missing

def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    return text.strip()

def extract_json_object(text: str) -> dict:
    """
    Robustly extract a JSON object from an LLM response.
    Tries multiple strategies before raising.
    """
    # Strategy 1: direct parse after stripping fences
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find first { ... } block
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3: locate outermost braces manually (handles trailing garbage)
    start = text.find("{")
    if start != -1:
        depth, end = 0, -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    raise ValueError(f"No valid JSON object found in LLM response:\n{text[:600]}")


def extract_json_array(text: str) -> list:
    """Robustly extract a JSON array from an LLM response."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON array found in LLM response:\n{text[:600]}")


def sanitize_column_name(sql_cols: List[str]) -> List[str]:
    """Replace every non-alphanumeric character in column names with ``_``."""
    all_chars = "".join(chr(i) for i in range(256))
    allowed   = string.ascii_letters + string.digits
    trans     = str.maketrans({c: "_" for c in all_chars if c not in allowed})
    return [col.translate(trans).strip("_") for col in sql_cols]

def _is_column_narrowing(sql: str) -> bool:
    """
    Return True when the query is a bare column-selection that would drop
    columns the pipeline still needs (e.g. ``SELECT `Name` FROM {prev}``).

    A query is narrowing when ALL of:
      - Single SELECT...FROM (no CTEs, no subqueries).
      - Selects named columns — NOT SELECT * or SELECT *,...
      - Has NO GROUP BY, HAVING, aggregation functions, DISTINCT, LIMIT,
        WHERE, or ORDER BY that give semantic meaning to the selection.

    In that case the LLM ignored the "SELECT *" instruction and we safely
    rewrite to SELECT * so all upstream columns are preserved.
    """
    s = sql.strip()
    if not re.match(r"^SELECT\s+", s, re.IGNORECASE):
        return False
    # Already wildcard
    if re.match(r"^SELECT\s+\*", s, re.IGNORECASE):
        return False
    # Has semantic clauses that justify explicit column listing
    semantic = re.compile(
        r"\b(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|WHERE|DISTINCT|"
        r"COUNT\s*\(|SUM\s*\(|AVG\s*\(|MIN\s*\(|MAX\s*\(|"
        r"UNION|INTERSECT|EXCEPT|JOIN)\b",
        re.IGNORECASE,
    )
    if semantic.search(s):
        return False
    # Contains a subquery
    if re.search(r"\(\s*SELECT\b", s, re.IGNORECASE):
        return False
    return True

def sanitize_llm_sql(sql: str) -> str:
    """
    Post-process every LLM-returned SQL string to fix known generation artifacts.

    1. Strip markdown fences
    2. Strip trailing semicolons
    3. Fix ``SELECT ,`` -> ``SELECT *,``
    4. Fix column-narrowing queries -> ``SELECT * FROM ...``

    Fix 4 is the core guard against the pipeline-breakage bug: when the LLM
    generates ``SELECT `Name` FROM {prev}`` despite being told "SELECT *
    unless aggregating", every downstream CTE step breaks because the other
    columns have been dropped.  Detecting and rewriting this BEFORE the query
    is stored means no rectification pass is ever needed for this class of
    error, and previewing any earlier step always works.
    """
    sql = re.sub(r"```(?:sql)?\s*", "", sql)
    sql = re.sub(r"```", "", sql).strip()
    sql = sql.rstrip(";").strip()
    sql = re.sub(r"\bSELECT\s+,", "SELECT *,", sql, flags=re.IGNORECASE)

    if _is_column_narrowing(sql):
        from_match = re.search(r"\bFROM\b", sql, re.IGNORECASE)
        if from_match:
            sql = "SELECT * " + sql[from_match.start():]

    return sql