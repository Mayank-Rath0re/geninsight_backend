# core/config.py
"""
Single source of truth for environment configuration.

Previously DB credentials were read directly inside db_handler.py and LLM
API keys were read directly inside llm_handler.py, with no shared place to
see what env vars the app depends on. Centralizing them here also lets
config validation fail fast on startup with one clear error instead of
wherever the first DB/LLM call happens to occur.
"""

import logging
import os
import sys

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_DATABASE = os.getenv("DB_DATABASE")

try:
    _db_port_env = os.getenv("DB_PORT")
    if not _db_port_env:
        raise ValueError("DB_PORT environment variable is missing.")
    DB_PORT = int(_db_port_env)
except (ValueError, TypeError) as e:
    logger.critical(f"Database credentials error: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "google/gemma-4-31b-it:standard"

# Optional but recommended by OpenRouter for attribution/rankings
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL", "")
OPENROUTER_SITE_NAME = os.getenv("OPENROUTER_SITE_NAME", "")

# Fallback sequence for Groq models, ordered to avoid rate-limit locks
GROQ_MODELS = [
    "llama-3.3-70b-versatile",                    # Primary (flagship reasoning)
    "meta-llama/llama-4-scout-17b-16e-instruct",   # Secondary (high TPM headroom)
    "qwen/qwen3-32b",                              # Tertiary fallback
]

DEFAULT_LLM_PROVIDER = "openrouter"  # "groq" | "openrouter"

# ---------------------------------------------------------------------------
# Auth (JWT)
# ---------------------------------------------------------------------------

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

if not JWT_SECRET_KEY:
    logger.critical("JWT_SECRET_KEY environment variable is missing.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Email (Resend / OTP)
# ---------------------------------------------------------------------------

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

UPLOAD_DIR = "./uploaded_datasets"
os.makedirs(UPLOAD_DIR, exist_ok=True)

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

ENV = os.getenv("ENV", "development")  # set ENV=production in your prod .env