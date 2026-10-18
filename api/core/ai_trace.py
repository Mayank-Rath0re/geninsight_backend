import json, logging, uuid

logger = logging.getLogger("my_global_app_logger")
_MAX = 8000


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def _clip(v):
    if isinstance(v, str) and len(v) > _MAX:
        return v[:_MAX] + f"...[+{len(v) - _MAX} chars]"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, default=str)
        return s if len(s) <= _MAX else s[:_MAX] + "...[clipped]"
    return v


def log_ai_event(trace_id: str, stage: str, level: str = "info", **fields):
    """One JSON line per event. grep 'AI_TRACE' and filter by trace_id/stage/error_code."""
    payload = {"trace_id": trace_id, "stage": stage, **{k: _clip(v) for k, v in fields.items()}}
    logger.log(
        logging.ERROR if level == "error" else logging.WARNING if level == "warn" else logging.INFO,
        "AI_TRACE %s", json.dumps(payload, default=str),
    )