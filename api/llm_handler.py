# llm_handler.py

from __future__ import annotations

import os
import logging
from typing import Literal, Optional

from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError as GeminiAPIError
from groq import Groq, APIStatusError, APIConnectionError

# Configure logger
logger = logging.getLogger("llm_handler")
logging.basicConfig(level=logging.INFO)

load_dotenv()

# --- Config ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GEMINI_MODEL = "gemini-3.5-flash"

# Fallback sequence for Groq models to avoid rate-limit locks
GROQ_MODELS = [
    "llama-3.3-70b-versatile",                       # Primary (Flagship reasoning)
    "meta-llama/llama-4-scout-17b-16e-instruct", # Secondary (High TPM headroom)
    "qwen/qwen3-32b"                             # Tertiary fallback
]

Provider = Literal["groq", "gemini"]
DEFAULT_PROVIDER: Provider = "groq"

# --- Lazy client singletons ---
_gemini_client: Optional[genai.Client] = None
_groq_client: Optional[Groq] = None


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        if not GOOGLE_API_KEY:
            raise RuntimeError("CRITICAL: GOOGLE_API_KEY environment variable is missing")
        _gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _gemini_client


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("CRITICAL: GROQ_API_KEY environment variable is missing")
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _resolve_provider(activate: Optional[Provider]) -> Provider:
    provider = activate or DEFAULT_PROVIDER
    if provider not in ("groq", "gemini"):
        raise ValueError(f"Unknown provider '{provider}'. Expected 'groq' or 'gemini'.")
    return provider


# --- Gemini backends ---
def _generate_response_gemini(prompt: str) -> str:
    try:
        response = _get_gemini_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        if not response.text:
            raise ValueError("Gemini returned an empty response.")
        return response.text
    except GeminiAPIError as e:
        logger.error(f"Gemini API Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in Gemini backend: {e}")
        raise


def _ask_llm_gemini(system_prompt: str, user_prompt: str) -> str:
    try:
        response = _get_gemini_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=system_prompt,
            ),
        )
        if not response.text:
            raise ValueError("Gemini returned an empty response.")
        return response.text
    except GeminiAPIError as e:
        logger.error(f"Gemini API Error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in Gemini backend: {e}")
        raise


# --- Groq backends with dynamic fallback and adaptive token budgeting ---
def _execute_groq_completion(messages: list[dict[str, str]]) -> str:
    """Executes a Groq chat completion.
    
    Iterates through candidate models and scales down max_completion_tokens 
    if a rate limit (status 413 or 429) is hit.
    """
    last_exception = None
    
    for model in GROQ_MODELS:
        # Dynamically allocate token budget:
        # For gpt-oss-120b (8k TPM limit), we must restrict max_completion_tokens 
        # to prevent immediate "Request too large" API rejection.
        is_large_model = "70b" in model
        max_tokens = 1500 if is_large_model else 4096
        
        try:
            logger.info(f"Attempting Groq completion with model: {model} (max_tokens: {max_tokens})")
            completion = _get_groq_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7, # Slightly lower for more consistent, production-safe outputs
                max_completion_tokens=max_tokens,
                top_p=1,
                #reasoning_effort="medium" if is_large_model else None, # Only supported on reasoning models
                stream=False,
                stop=None,
            )
            
            content = completion.choices[0].message.content
            if not content:
                raise ValueError(f"Groq model {model} returned an empty response.")
            return content

        except APIStatusError as e:
            # Catch 413 (Payload/Request Too Large) or 429 (Rate Limit / TPM exceeded)
            if e.status_code in (413, 429):
                logger.warning(
                    f"Groq model {model} hit limits (Status {e.status_code}). "
                    f"Message: {e.message}. Trying next fallback model..."
                )
                last_exception = e
                continue
            else:
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


def _generate_response_groq(prompt: str) -> str:
    messages = [{"role": "user", "content": prompt}]
    return _execute_groq_completion(messages)


def _ask_llm_groq(system_prompt: str, user_prompt: str) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return _execute_groq_completion(messages)


# --- Public Resilient API ---
def generate_response(prompt: str, activate: Optional[Provider] = None) -> str:
    """Generates a standard response using the selected provider.
    
    If the primary provider fails, automatically falls back to the other
    to guarantee high-availability.
    """
    provider = _resolve_provider(activate)
    
    if provider == "groq":
        try:
            return _generate_response_groq(prompt)
        except Exception as e:
            logger.error(f"Groq run failed: {e}. Cascading to Gemini fallback...")
            return _generate_response_gemini(prompt)
            
    # Default path for Gemini
    try:
        return _generate_response_gemini(prompt)
    except Exception as e:
        logger.error(f"Gemini run failed: {e}. Cascading to Groq fallback...")
        return _generate_response_groq(prompt)


def ask_llm(system_prompt: str, user_prompt: str, activate: Optional[Provider] = None) -> str:
    """Generates a structured response using the selected provider.
    
    If the primary provider fails, automatically falls back to the other.
    """
    provider = _resolve_provider(activate)
    
    if provider == "groq":
        try:
            return _ask_llm_groq(system_prompt, user_prompt)
        except Exception as e:
            logger.error(f"Groq run failed: {e}. Cascading to Gemini fallback...")
            return _ask_llm_gemini(system_prompt, user_prompt)
            
    # Default path for Gemini
    try:
        return _ask_llm_gemini(system_prompt, user_prompt)
    except Exception as e:
        logger.error(f"Gemini run failed: {e}. Cascading to Groq fallback...")
        return _ask_llm_groq(system_prompt, user_prompt)