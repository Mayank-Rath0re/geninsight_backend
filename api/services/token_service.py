# token_service.py
"""
JWT access/refresh token creation and verification.

FIXED: decode_token() called `jwt.decode(token, SECRET_KEY,
algorithm=[ALGORITHM])`. PyJWT's decode() takes the keyword `algorithms`
(plural, a list of allowed algorithms) — `algorithm` (singular) is not a
recognized kwarg for decode() and raises a TypeError, which would have
crashed every /refresh and /logout call that reached decode_token().
"""

import jwt
from datetime import datetime, timedelta, timezone

from core.config import JWT_SECRET_KEY as SECRET_KEY, JWT_ALGORITHM as ALGORITHM

ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS_DEFAULT = 1
REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER = 30


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict, remember_me: bool = False) -> str:
    to_encode = data.copy()
    days = REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER if remember_me else REFRESH_TOKEN_EXPIRE_DAYS_DEFAULT
    expire = datetime.now(timezone.utc) + timedelta(days=days)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")
