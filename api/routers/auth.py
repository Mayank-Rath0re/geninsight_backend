# routers/auth.py
"""
Full auth flow: signup + OTP verification, login (access + refresh
tokens), refresh, logout, and forgot/reset password.
"""

from datetime import datetime, timedelta, timezone
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

from core import db
from core.security import hash_password, verify_password
from services import email_service
from services.token_service import create_access_token, create_refresh_token, decode_token
from core import config

router = APIRouter(tags=["auth"])


# ─────────────────────────────────────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: EmailStr
    name: str
    password: str


@router.post("/signup")
def signup(payload: SignupRequest):
    check_query = "SELECT id, is_verified FROM auth WHERE email = %s"
    existing = db.fetch(check_query, (payload.email,))

    if existing:
        _user_id, is_verified = existing[0][0], existing[0][1]
        if is_verified:
            raise HTTPException(status_code=400, detail="Account already exists. Please log in.")
        # Stale unverified signup - overwrite it
        hashed = hash_password(payload.password)
        update_query = """
            UPDATE auth
            SET name = %s, password_hash = %s
            WHERE email = %s
        """
        db.run(update_query, (payload.name, hashed, payload.email))
    else:
        hashed = hash_password(payload.password)
        insert_query = """
            INSERT INTO auth(email, name, password_hash, is_verified)
            VALUES(%s, %s, %s, 0)
        """
        db.run(insert_query, (payload.email, payload.name, hashed))

    email_service.send_otp_mail(payload.email, usageCode="signup")

    return {"message": "OTP sent to your email. Please verify to complete signup."}


# ─────────────────────────────────────────────────────────────────────────
# OTP VERIFICATION (signup)
# ─────────────────────────────────────────────────────────────────────────

class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str


@router.post("/verify-otp")
def verify_otp(payload: VerifyOtpRequest):
    is_valid = email_service.verify_mail(payload.email, payload.otp, usageCode="signup")
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")

    update_query = "UPDATE auth SET is_verified = 1 WHERE email = %s"
    db.run(update_query, (payload.email,))

    return {"message": "Email verified successfully. You can now log in."}


# ─────────────────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    remember_me: bool = False


@router.post("/login")
def login(payload: LoginRequest, response: Response):
    query = "SELECT id, password_hash, is_verified FROM auth WHERE email = %s"
    result = db.fetch(query, (payload.email,))

    if not result:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    user_id, password_hash, is_verified = result[0][0], result[0][1], result[0][2]

    if not is_verified:
        raise HTTPException(status_code=403, detail="Please verify your email before logging in.")

    if not verify_password(payload.password, password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token_data = {"sub": payload.email, "user_id": user_id}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data, remember_me=payload.remember_me)

    days = 30 if payload.remember_me else 1
    refresh_expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    # Store refresh token (overwrites old one -> kills old session)
    update_query = """
        UPDATE auth
        SET refresh_token = %s, refresh_token_expires_at = %s
        WHERE id = %s
    """
    db.run(update_query, (refresh_token, refresh_expires_at, user_id))

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=(config.ENV == "production"),
        samesite="lax" if config.ENV != "production" else "strict",
        max_age=days * 24 * 60 * 60,
        path="/",
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "message": "Login successful",
    }


# ─────────────────────────────────────────────────────────────────────────
# REFRESH
# ─────────────────────────────────────────────────────────────────────────

@router.post("/refresh")
def refresh(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")

    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token provided.")

    try:
        payload = decode_token(refresh_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type.")

    user_id = payload.get("user_id")
    email = payload.get("sub")

    query = "SELECT refresh_token, refresh_token_expires_at FROM auth WHERE id = %s"
    result = db.fetch(query, (user_id,))

    if not result:
        raise HTTPException(status_code=401, detail="User not found.")

    stored_token, expires_at = result[0][0], result[0][1]

    if stored_token != refresh_token:
        # Token doesn't match what's on file -> was revoked, or a new login happened elsewhere
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

    new_access_token = create_access_token({"sub": email, "user_id": user_id})

    return {"access_token": new_access_token, "token_type": "bearer"}


# ─────────────────────────────────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────────────────────────────────

@router.post("/logout")
def logout(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")

    if refresh_token:
        try:
            payload = decode_token(refresh_token)
            user_id = payload.get("user_id")

            if user_id:
                update_query = """
                    UPDATE auth
                    SET refresh_token = NULL, refresh_token_expires_at = NULL
                    WHERE id = %s
                """
                db.run(update_query, (user_id,))
        except ValueError:
            pass

    response.delete_cookie(key="refresh_token", path="/")

    return {"message": "Logged out successfully."}


# ─────────────────────────────────────────────────────────────────────────
# FORGOT PASSWORD
# ─────────────────────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest):
    query = "SELECT id FROM auth WHERE email = %s and is_verified = 1"
    result = db.fetch(query, (payload.email,))

    if not result:
        # Don't reveal whether the email exists - same generic response either way
        return {"message": "If this email is registered, a reset code has been sent."}

    email_service.send_otp_mail(payload.email, usageCode="reset-password")

    return {"message": "If this email is registered, a reset code has been sent."}


class VerifyResetOtpRequest(BaseModel):
    email: EmailStr
    otp: str


@router.post("/verify-reset-otp")
def verify_reset_otp(payload: VerifyResetOtpRequest):
    is_valid = email_service.verify_mail(payload.email, payload.otp, usageCode="reset-password")

    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP.")

    query = "SELECT id FROM auth WHERE email = %s"
    result = db.fetch(query, (payload.email,))
    if not result:
        raise HTTPException(status_code=404, detail="User not found.")
    user_id = result[0][0]

    # Generate a secure random reset token (not a JWT - just an opaque random string)
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    insert_query = """
        INSERT INTO password_reset_tokens(user_id, token, expires_at, used)
        VALUES(%s, %s, %s, 0)
    """
    db.run(insert_query, (user_id, reset_token, expires_at))

    return {"reset_token": reset_token}


class ResetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest):
    query = """
        SELECT user_id, expires_at, used
        FROM password_reset_tokens
        WHERE token = %s
    """
    result = db.fetch(query, (payload.reset_token,))

    if not result:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

    user_id, expires_at, used = result[0][0], result[0][1], result[0][2]

    if used:
        raise HTTPException(status_code=400, detail="This reset link has already been used.")

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="This reset link has expired.")

    new_hashed = hash_password(payload.new_password)

    # Update password AND kill any existing session
    update_query = """
        UPDATE auth
        SET password_hash = %s, refresh_token = NULL, refresh_token_expires_at = NULL
        WHERE id = %s
    """
    db.run(update_query, (new_hashed, user_id))

    mark_used_query = "UPDATE password_reset_tokens SET used = 1 WHERE token = %s"
    db.run(mark_used_query, (payload.reset_token,))

    return {"message": "Password reset successful. Please log in with your new password."}
