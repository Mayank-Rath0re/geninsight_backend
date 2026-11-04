# services/llm_client.py
"""
LLM client (was llm_handler.py).

Token metering: every SUCCESSFUL provider call reports its input/output tokens through
core.usage_context.record_llm_usage(). Failed or rate-limited attempts that fall through to
the next model/provider are not charged. If a provider omits `usage`, tokens are estimated at
~4 characters per token so usage is never silently free.

NOTE: The original file also defined `generate_response()`; nothing called it, so it stays removed.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from openai import OpenAI
from openai import APIStatusError as OpenRouterAPIStatusError
from openai import APIConnectionError as OpenRouterAPIConnectionError
from groq import Groq, APIStatusError, APIConnectionError

from core.config import (
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    OPENROUTER_SITE_URL,
    OPENROUTER_SITE_NAME,
    GROQ_API_KEY,
    GROQ_MODELS,
    DEFAULT_LLM_PROVIDER,
)
from core.usage_context import record_llm_usage

logger = logging.getLogger("llm_client")

Provider = Literal["groq", "openrouter"]

# --- Lazy client singletons ---
_openrouter_client: Optional[OpenAI] = None
_groq_client: Optional[Groq] = None


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


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("CRITICAL: GROQ_API_KEY environment variable is missing")
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _resolve_provider(activate: Optional[Provider]) -> Provider:
    provider = activate or DEFAULT_LLM_PROVIDER
    if provider not in ("groq", "openrouter"):
        raise ValueError(f"Unknown provider '{provider}'. Expected 'groq' or 'openrouter'.")
    return provider


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


# --- OpenRouter backend ---

def _ask_llm_openrouter(system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        completion = _get_openrouter_client().chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
        )
        content = completion.choices[0].message.content
        if not content:
            raise ValueError("OpenRouter returned an empty response.")
        _meter(completion, messages, content)
        return content
    except (OpenRouterAPIStatusError, OpenRouterAPIConnectionError) as e:
        logger.error(f"OpenRouter API Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in OpenRouter backend: {e}")
        raise


# --- Groq backend with dynamic fallback and adaptive token budgeting ---

def _execute_groq_completion(messages: list[dict[str, str]]) -> str:
    """
    Iterates through candidate models and scales down max_completion_tokens
    if a rate limit (status 413 or 429) is hit.
    """
    last_exception = None

    for model in GROQ_MODELS:
        # gpt-oss-120b-class models have an 8k TPM limit, so we restrict
        # max_completion_tokens for large models to avoid immediate
        # "Request too large" API rejection.
        is_large_model = "70b" in model
        max_tokens = 1500 if is_large_model else 4096

        try:
            logger.info(f"Attempting Groq completion with model: {model} (max_tokens: {max_tokens})")
            completion = _get_groq_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,  # slightly lower for consistent, production-safe outputs
                max_completion_tokens=max_tokens,
                top_p=1,
                stream=False,
                stop=None,
            )

            content = completion.choices[0].message.content
            if not content:
                raise ValueError(f"Groq model {model} returned an empty response.")
            _meter(completion, messages, content)
            return content

        except APIStatusError as e:
            if e.status_code in (413, 429):
                logger.warning(
                    f"Groq model {model} hit limits (Status {e.status_code}). "
                    f"Message: {e.message}. Trying next fallback model..."
                )
                last_exception = e
                continue
            logger.error(f"Groq API Error status {e.status_code}: {e.message}")
            raise e
        except APIConnectionError as e:
            logger.warning(f"Groq network connection failed for model {model}: {e}. Retrying next model...")
            last_exception = e
            continue
        except Exception as e:
            logger.error(f"Unexpected exception during Groq dispatch: {e}")
            raise e

    raise RuntimeError(f"All configured Groq models failed. Last error: {last_exception}")


def _ask_llm_groq(system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return _execute_groq_completion(messages)


# --- Public API ---

def ask_llm(system_prompt: str, user_prompt: str, activate: Optional[Provider] = None) -> str:
    """
    Generates a structured response using the selected provider. If the
    primary provider fails, automatically falls back to the other for
    high availability.
    """
    provider = _resolve_provider(activate)

    if provider == "groq":
        try:
            return _ask_llm_groq(system_prompt, user_prompt)
        except Exception as e:
            logger.error(f"Groq run failed: {e}. Cascading to OpenRouter fallback...")
            return _ask_llm_openrouter(system_prompt, user_prompt)

    try:
        return _ask_llm_openrouter(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"OpenRouter run failed: {e}. Cascading to Groq fallback...")
        return _ask_llm_groq(system_prompt, user_prompt)
