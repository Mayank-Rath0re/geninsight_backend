# core/crypto.py
"""
Symmetric encryption for at-rest secrets (custom database passwords).

DB_CREDENTIALS_KEY should be a valid Fernet key. If it is unset (or not a valid
Fernet key), a deterministic key is derived from JWT_SECRET_KEY so the app keeps
working; rotating JWT_SECRET_KEY then invalidates previously stored secrets.
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from core import config

logger = logging.getLogger(__name__)

_fernet = None


def _build_fernet() -> Fernet:
    raw = os.getenv("DB_CREDENTIALS_KEY") or config.DB_CREDENTIALS_KEY
    if raw:
        try:
            return Fernet(raw.encode())
        except (ValueError, TypeError):
            logger.warning(
                "DB_CREDENTIALS_KEY is not a valid Fernet key; deriving one from JWT_SECRET_KEY instead."
            )
    if not config.JWT_SECRET_KEY:
        raise RuntimeError("Cannot derive a credential-encryption key without JWT_SECRET_KEY.")
    digest = hashlib.sha256(config.JWT_SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def encrypt_secret(plain: str) -> str:
    if plain is None:
        plain = ""
    return _cipher().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a stored secret. Returns the input unchanged when it is not a
    valid Fernet token, so manually seeded development rows keep working."""
    if not token:
        return ""
    try:
        return _cipher().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return token


def looks_encrypted(token: str) -> bool:
    try:
        _cipher().decrypt((token or "").encode())
        return True
    except (InvalidToken, ValueError, TypeError):
        return False
