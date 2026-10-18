import re

_FENCE = re.compile(r"```(?:sql)?", re.I)


def mask_sql(sql: str, nested: bool) -> str:
    """Blank out string literals and [bracketed] identifiers. If nested=True, also blank
    everything inside parentheses, so only top-level (depth 0) text remains."""
    out, depth, in_str, in_br = [], 0, False, False
    for ch in sql:
        if in_str:
            out.append(" ")
            in_str = ch != "'"
        elif in_br:
            out.append(" ")
            in_br = ch != "]"
        elif ch == "'":
            in_str = True; out.append(" ")
        elif ch == "[":
            in_br = True; out.append(" ")
        elif ch in "()":
            if nested:
                depth = depth + 1 if ch == "(" else max(depth - 1, 0)
                out.append(" ")
            else:
                out.append(ch)
        else:
            out.append(ch if (not nested or depth == 0) else " ")
    return "".join(out)


def ensure_order_by_safe(sql: str) -> str:
    """Top-level ORDER BY without TOP/OFFSET is illegal once the query is embedded as a
    CTE/derived table (Msg 1033). Appending OFFSET 0 ROWS makes it legal and changes nothing."""
    top = mask_sql(sql, nested=True)
    matches = list(re.finditer(r"\bORDER\s+BY\b", top, re.I))
    if not matches or re.search(r"\bTOP\b", top, re.I):
        return sql
    if re.search(r"\b(OFFSET|FOR\s+XML)\b", top[matches[-1].end():], re.I):
        return sql
    return sql + " OFFSET 0 ROWS"


def sanitize_sql(sql: str) -> str:
    sql = _FENCE.sub("", sql).strip().rstrip(";").strip()
    sql = re.sub(r"\bSELECT\s+,", "SELECT *,", sql, flags=re.I)
    return ensure_order_by_safe(sql)


_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|EXEC|EXECUTE|MERGE|INTO)\b", re.I
)


def is_safe_select(sql: str) -> tuple[bool, str]:
    flat = mask_sql(sql, nested=False)
    if not re.match(r"\s*(SELECT|WITH)\b", flat, re.I):
        return False, "not a SELECT/WITH statement"
    if ";" in flat:
        return False, "multiple statements"
    m = _FORBIDDEN.search(flat)
    return (False, f"forbidden keyword {m.group(1)}") if m else (True, "")


_ERROR_PATTERNS = [
    ("SQL_ORDER_BY", r"ORDER BY clause is invalid|\b1033\b", True),
    ("SQL_SYNTAX", r"Incorrect syntax|\b(102|156)\b", True),
    ("SQL_INVALID_COLUMN", r"Invalid column name|Invalid object name|\b(207|208)\b", True),
    ("SQL_GROUP_BY", r"not contained in either an aggregate|\b8120\b", True),
    ("SQL_CONVERSION", r"Conversion failed|Error converting|\b(245|8114)\b", True),
    ("SQL_DIVIDE_BY_ZERO", r"Divide by zero|\b8134\b", True),
    ("SQL_TIMEOUT", r"timeout|timed out", False),
]


def classify_sql_error(exc: Exception) -> dict:
    msg = str(exc)
    for code, pat, retryable in _ERROR_PATTERNS:
        if re.search(pat, msg, re.I):
            return {"code": code, "retryable": retryable}
    return {"code": "UNKNOWN", "retryable": False}