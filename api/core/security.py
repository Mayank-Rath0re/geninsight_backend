# security.py
"""
Password hashing utilities.

We SHA-256 the raw password before handing it to bcrypt because bcrypt
silently truncates any input past 72 bytes — without the pre-hash, two
different long passwords that share the same first 72 bytes would hash
identically. SHA-256 first means the full password always contributes to
the final bcrypt hash regardless of length.

FIXED: hash_password() computed `pre_hashed` but then called
`pwd_context.hash()` with no argument at all — a TypeError that would have
crashed every signup and every password reset. verify_password() already
correctly pre-hashes before comparing, so hash_password() needed to mirror
that exact same pre-hash step or valid logins would never match.
"""

import hashlib

from fastapi import Header, HTTPException
from passlib.context import CryptContext

from services.token_service import decode_token

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    pre_hashed = hashlib.sha256(password.encode()).hexdigest()
    return pwd_context.hash(pre_hashed)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pre_hashed = hashlib.sha256(plain_password.encode()).hexdigest()
    return pwd_context.verify(pre_hashed, hashed_password)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def get_current_user_id(authorization: str = Header(...)) -> int:
    """
    FastAPI dependency: validates the Bearer access token and returns the
    authenticated user_id. Raises 401 on any missing/invalid/expired token.
    Endpoints that also take a userId param must additionally check it
    matches this value (see core.security.require_same_user).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type.")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token missing user_id claim.")
    return user_id


def require_same_user(authenticated_user_id: int, requested_user_id: int) -> None:
    """Raises 401 if the caller's own userId param doesn't match their token."""
    if authenticated_user_id != requested_user_id:
        raise HTTPException(status_code=401, detail="User Mismatch")
