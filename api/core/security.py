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

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    pre_hashed = hashlib.sha256(password.encode()).hexdigest()
    return pwd_context.hash(pre_hashed)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pre_hashed = hashlib.sha256(plain_password.encode()).hexdigest()
    return pwd_context.verify(pre_hashed, hashed_password)
