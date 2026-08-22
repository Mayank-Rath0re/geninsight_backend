# core/json_utils.py
"""
Robust JSON extraction from LLM text responses (was part of helper.py).

Kept separate from ingestion.py because this is generic text-parsing logic
with no dependency on pandas/dataframes, used by every LLM-calling service
(ingestion, query_generation, dashboard).
"""

import json
import re


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` fences."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    return text.strip()


def extract_json_object(text: str) -> dict:
    """Robustly extract a JSON object from an LLM response, trying several strategies."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Locate outermost braces manually (handles trailing garbage after valid JSON)
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
                return json.loads(text[start: end + 1])
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
