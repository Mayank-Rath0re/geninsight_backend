# core/config.py
"""
Single source of truth for environment configuration.
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
    "qwen/qwen3.8-27b",      # Primary (flagship reasoning)
    "openai/gpt-oss-120b",   # Secondary (high TPM headroom)
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
# Uploads
# ---------------------------------------------------------------------------

UPLOAD_DIR = "./uploaded_datasets"
os.makedirs(UPLOAD_DIR, exist_ok=True)

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

ENV = os.getenv("ENV", "development")  # set ENV=production in your prod .env

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Comma-separated exact origins, e.g. "https://app.example.com,https://www.example.com".
# Wildcard "*" cannot be combined with credentials (the refresh-token cookie), so in
# development any localhost port is allowed via a regex instead (Flutter web picks random ports).
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
CORS_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1)(:\d+)?" if ENV != "production" else None

# ---------------------------------------------------------------------------
# Billing (Razorpay)
# ---------------------------------------------------------------------------
# KEY_ID is public (sent to the client at checkout). KEY_SECRET and WEBHOOK_SECRET never leave
# the server. Plan ids come from Razorpay Dashboard > Subscriptions > Plans.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_PLAN_IDS = {
    "individual": os.getenv("RAZORPAY_PLAN_INDIVIDUAL", ""),
    "maxx": os.getenv("RAZORPAY_PLAN_MAXX", ""),
    "enterprise": os.getenv("RAZORPAY_PLAN_ENTERPRISE", ""),
}

if ENV == "production" and not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET):
    logger.warning("Razorpay is not fully configured; paid checkout and webhooks will fail.")

# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
# Run the retention/purge scheduler in exactly ONE process. With several workers, set
# RUN_SCHEDULER=0 on all but one.
RUN_SCHEDULER = os.getenv("RUN_SCHEDULER", "1") == "1"
PURGE_GRACE_DAYS = int(os.getenv("PURGE_GRACE_DAYS", "30"))      # hidden -> physically dropped
DOWNGRADE_GRACE_DAYS = int(os.getenv("DOWNGRADE_GRACE_DAYS", "30"))  # no retention deletes right after a plan lapses

# ---------------------------------------------------------------------------
# Custom (client-owned) databases
# ---------------------------------------------------------------------------
# Fernet key used to encrypt stored DB passwords. If unset, a key is derived from
# JWT_SECRET_KEY — rotating JWT_SECRET_KEY then makes existing stored credentials
# unreadable, so set a dedicated DB_CREDENTIALS_KEY in production.
DB_CREDENTIALS_KEY = os.getenv("DB_CREDENTIALS_KEY", "")

CUSTOM_DB_CONNECT_TIMEOUT = int(os.getenv("CUSTOM_DB_CONNECT_TIMEOUT", "10"))
CUSTOM_DB_QUERY_TIMEOUT = int(os.getenv("CUSTOM_DB_QUERY_TIMEOUT", "30"))
# Hard cap on rows returned by a query against a custom DB (protects our process and
# the client's database from unbounded result sets).
CUSTOM_DB_MAX_ROWS = int(os.getenv("CUSTOM_DB_MAX_ROWS", "5000"))
# Rows sampled per table when building its metadata/knowledge base at registration.
CUSTOM_DB_SAMPLE_ROWS = int(os.getenv("CUSTOM_DB_SAMPLE_ROWS", "50"))
CUSTOM_DB_MAX_TABLES_PER_SOURCE = int(os.getenv("CUSTOM_DB_MAX_TABLES_PER_SOURCE", "200"))

# Block private/internal addresses for custom DB hosts unless explicitly allowed.
# Defaults to allowed in development (local DBs) and blocked in production (SSRF guard).
ALLOW_PRIVATE_DB_HOSTS = os.getenv(
    "ALLOW_PRIVATE_DB_HOSTS", "0" if ENV == "production" else "1"
) == "1"
