# services/llm_client.py
"""
LLM client (OpenRouter only).

Model routing is plan-based: Free -> Gemma, paid (individual/maxx/enterprise) ->
Claude Haiku. The plan code comes from core.usage_context, set by
services.usage_service.metered_call().

Token metering: every SUCCESSFUL provider call reports its input/output tokens through
core.usage_context.record_llm_usage(). Failed attempts that fall through to the next
model are not charged. If a provider omits `usage`, tokens are estimated at
~4 characters per token so usage is never silently free.
"""

from __future__ import annotations

import logging
from typing import Optional

from openai import OpenAI
from openai import APIStatusError, APIConnectionError

from core.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_FREE_MODELS,
    OPENROUTER_PAID_MODELS,
    OPENROUTER_SITE_URL,
    OPENROUTER_SITE_NAME,
)
from core.usage_context import current_plan_code, record_llm_usage

logger = logging.getLogger("llm_client")

# --- Lazy client singleton ---
_openrouter_client: Optional[OpenAI] = None


def _get_openrouter_client() -> OpenAI:
    global _openrouter_client
    if _openrouter_client is None:
        if not OPENROUTER_API_KEY:
            raise RuntimeError("CRITICAL: OPENROUTER_API_KEY environment variable is missing")
        default_headers = {}
        if OPENROUTER_SITE_URL:
            default_headers["HTTP-Referer"] = OPENROUTER_SITE_URL
        if OPENROUTER_SITE_NAME:
            default_headers["X-Title"] = OPENROUTER_SITE_NAME
        _openrouter_client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=OPENROUTER_API_KEY,
            default_headers=default_headers or None,
        )
    return _openrouter_client


# --- Usage metering ---

def _meter(completion, messages: list[dict[str, str]], content: str) -> None:
    """Report tokens for one successful completion (never raises)."""
    try:
        usage = getattr(completion, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is None or completion_tokens is None:
            prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 4
            completion_tokens = len(content) // 4
        record_llm_usage(prompt_tokens, completion_tokens)
    except Exception:
        logger.exception("Failed to record LLM usage")


# --- OpenRouter backend with model fallback ---

def _models_for_plan(plan_code: Optional[str]) -> list[dict]:
    """Paid plans get Claude Haiku; Free (and unknown/background work) gets Gemma."""
    if plan_code and plan_code.lower() != "free":
        return OPENROUTER_PAID_MODELS
    return OPENROUTER_FREE_MODELS


def _ask_llm_openrouter(system_prompt: str, user_prompt: str) -> str:
    """Walks the plan's model chain in order and returns the first successful completion."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    models = _models_for_plan(current_plan_code())
    last_exception: Optional[Exception] = None

    for cfg in models:
        model = cfg["model"]
        extra_body = cfg.get("extra_body")
        try:
            logger.info(f"Attempting OpenRouter completion with model: {model}")
            kwargs = {"model": model, "messages": messages}
            if extra_body:
                kwargs["extra_body"] = extra_body

            completion = _get_openrouter_client().chat.completions.create(**kwargs)
            content = completion.choices[0].message.content
            if not content:
                raise ValueError(f"OpenRouter model {model} returned an empty response.")
            _meter(completion, messages, content)
            return content

        except (APIStatusError, APIConnectionError, ValueError) as e:
            logger.warning(f"OpenRouter model {model} failed: {e}. Trying next model...")
            last_exception = e
            continue
        except Exception as e:
            logger.error(f"Unexpected error in OpenRouter backend ({model}): {e}")
            raise

    raise RuntimeError(f"All configured OpenRouter models failed. Last error: {last_exception}")


# --- Public API ---

def ask_llm(system_prompt: str, user_prompt: str, activate: Optional[str] = None) -> str:
    """
    Generates a response via OpenRouter using the model chain for the caller's plan
    (read from core.usage_context; paid -> Claude Haiku, free -> Gemma).

    `activate` is kept only so existing call sites don't break; Groq has been removed,
    so any value is ignored (a warning is logged if it isn't "openrouter").
    """
    if activate not in (None, "openrouter"):
        logger.warning(f"Provider '{activate}' is no longer supported; using OpenRouter.")
    return _ask_llm_openrouter(system_prompt, user_prompt)