# services/referral_service.py
"""
Voucher / referral codes.

A code is an admin-defined grant: duration_days of a paid plan, redeemable by at
most max_redemptions distinct users. Redeeming creates a normal `subscriptions`
row flagged is_referral = 1 with a synthetic razorpay_subscription_id (no money
moves), plus a `referral_redemptions` audit row.

Expiry: a referral subscription ends exactly at current_period_end — there is no
renewal grace. `get_entitlement` already ignores it after that instant, so the
user drops to Free immediately; `run_expiry_sweep` only tidies the rows. The UI
gets the full trial state (and what happens after it) from `get_trial` /
`preview`.

Redemption also retires the testing-phase Maxx grant (is_test_grant = 1);
otherwise the voucher's Individual plan would be outranked by the free Maxx.
"""

import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from core import config, db
from services import billing_service
from services.billing_service import utcnow
from services.usage_service import iso

logger = logging.getLogger("my_global_app_logger")

_CODE_SELECT = (
    "SELECT c.id, c.code, c.plan_id, c.duration_days, c.max_redemptions, "
    "c.redeemed_count, c.is_active, c.valid_until, c.note "
    "FROM referral_codes c WHERE c.code = %s"
)


def normalise(code: str) -> str:
    return (code or "").strip().upper()


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _fallback() -> dict:
    """The plan the user lands on when the trial ends, plus the downgrade windows."""
    free = billing_service.get_plan_by_code("free")
    return {
        "plan": free.to_dict() if free else None,
        "downgrade_grace_days": int(config.DOWNGRADE_GRACE_DAYS),
        "purge_grace_days": int(config.PURGE_GRACE_DAYS),
    }


def _check_code(row, now: datetime) -> None:
    """Raise unless the code row can still be redeemed by anyone."""
    if row is None:
        raise _error(404, "REFERRAL_NOT_FOUND", "That voucher code doesn't exist.")
    if not bool(row[6]):
        raise _error(410, "REFERRAL_INACTIVE", "This voucher code is no longer active.")
    if row[7] is not None and now >= row[7]:
        raise _error(410, "REFERRAL_CODE_EXPIRED", "This voucher code has expired.")
    if row[4] is not None and row[5] >= row[4]:
        raise _error(409, "REFERRAL_EXHAUSTED", "This voucher code has been fully redeemed.")


def _assert_can_redeem(user_id: int, now: datetime) -> None:
    ent = billing_service.get_entitlement(user_id)
    if ent.subscription_id is not None and not ent.is_referral and not ent.is_test_grant:
        raise _error(409, "REFERRAL_PAID_PLAN",
                     "You already have a paid plan. Voucher trials are for accounts without one.")
    active = db.fetch(
        "SELECT TOP 1 expires_at FROM referral_redemptions "
        "WHERE user_id = %s AND status = 'active' AND expires_at > %s",
        (user_id, now),
    )
    if active:
        raise _error(409, "REFERRAL_ACTIVE", "You already have a voucher trial running.")


def _plan_for(row):
    plan = billing_service.get_plan_by_id(row[2])
    if plan is None or plan.code == "free":
        raise _error(410, "REFERRAL_INVALID_PLAN", "This voucher is not redeemable.")
    return plan


def preview(user_id: int, code: str) -> dict:
    """Validate a code and describe exactly what redeeming it would do."""
    now = utcnow()
    row = None
    rows = db.fetch(_CODE_SELECT, (normalise(code),))
    if rows:
        row = rows[0]
    _check_code(row, now)
    plan = _plan_for(row)
    _assert_can_redeem(user_id, now)
    remaining = None if row[4] is None else int(row[4]) - int(row[5])
    return {
        "code": row[1],
        "plan": plan.to_dict(),
        "duration_days": int(row[3]),
        "expires_at": iso(now + timedelta(days=int(row[3]))),
        "remaining_redemptions": remaining,
        "note": row[8],
        "fallback": _fallback(),
    }


def redeem(user_id: int, code: str) -> dict:
    """Atomically consume one redemption and start the trial. Returns the new trial."""
    norm = normalise(code)
    if not norm:
        raise _error(400, "REFERRAL_CODE_REQUIRED", "Enter a voucher code.")
    now = utcnow()
    _assert_can_redeem(user_id, now)
    fallback = _fallback()

    with db.transaction() as cur:
        # UPDLOCK+HOLDLOCK serialises concurrent redemptions of the same code.
        cur.execute(
            "SELECT c.id, c.code, c.plan_id, c.duration_days, c.max_redemptions, "
            "c.redeemed_count, c.is_active, c.valid_until, c.note "
            "FROM referral_codes c WITH (UPDLOCK, HOLDLOCK) WHERE c.code = %s",
            (norm,),
        )
        row = cur.fetchone()
        _check_code(row, now)
        code_id, plan_id, duration = row[0], row[2], int(row[3])
        plan = _plan_for(row)

        cur.execute(
            "SELECT 1 FROM referral_redemptions WHERE user_id = %s AND code_id = %s",
            (user_id, code_id),
        )
        if cur.fetchone():
            raise _error(409, "REFERRAL_ALREADY_REDEEMED",
                         "You have already used this voucher code.")

        cur.execute(
            "UPDATE referral_codes SET redeemed_count = redeemed_count + 1, "
            "updated_at = SYSUTCDATETIME() "
            "WHERE id = %s AND (max_redemptions IS NULL OR redeemed_count < max_redemptions)",
            (code_id,),
        )
        if cur.rowcount != 1:
            raise _error(409, "REFERRAL_EXHAUSTED", "This voucher code has been fully redeemed.")

        # A paid upgrade also retires a trial, but be defensive: clear any stale one.
        cur.execute(
            "UPDATE referral_redemptions SET status = 'expired', "
            "ended_at = COALESCE(ended_at, SYSUTCDATETIME()) "
            "WHERE user_id = %s AND status = 'active'",
            (user_id,),
        )
        cur.execute(
            "UPDATE subscriptions SET status = 'expired', cancel_at_period_end = 0, "
            "updated_at = SYSUTCDATETIME() "
            "WHERE owner_user_id = %s AND is_referral = 1 AND status = 'active'",
            (user_id,),
        )
        # Testing-phase Maxx would outrank Individual; retire it so the voucher applies.
        cur.execute(
            "UPDATE subscriptions SET status = 'expired', current_period_end = SYSUTCDATETIME(), "
            "cancel_at_period_end = 0, updated_at = SYSUTCDATETIME() "
            "WHERE owner_user_id = %s AND is_test_grant = 1 AND status = 'active'",
            (user_id,),
        )

        expires_at = now + timedelta(days=duration)
        cur.execute(
            "INSERT INTO subscriptions (owner_user_id, org_id, plan_id, seats, status, "
            "razorpay_subscription_id, current_period_start, current_period_end, "
            "cancel_at_period_end, is_test_grant, is_referral) "
            "OUTPUT INSERTED.id VALUES (%s, NULL, %s, 1, 'active', %s, %s, %s, 1, 0, 1)",
            (user_id, plan_id, f"REFERRAL-{uuid.uuid4().hex}", now, expires_at),
        )
        subscription_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO referral_redemptions (code_id, user_id, subscription_id, status, "
            "redeemed_at, expires_at) "
            "OUTPUT INSERTED.id VALUES (%s, %s, %s, 'active', %s, %s)",
            (code_id, user_id, subscription_id, now, expires_at),
        )
        redemption_id = cur.fetchone()[0]

    logger.info("Referral %s redeemed by user %s (%s days of %s)",
                norm, user_id, duration, plan.code)
    return {
        "redemption_id": redemption_id,
        "code": norm,
        "note": row[8],
        "plan": plan.to_dict(),
        "duration_days": duration,
        "started_at": iso(now),
        "expires_at": iso(expires_at),
        "fallback": fallback,
    }


def get_trial(user_id: int) -> Optional[dict]:
    """Most recent redemption for the user, with expiry resolved lazily."""
    now = utcnow()
    rows = db.fetch(
        "SELECT TOP 1 r.id, r.status, r.redeemed_at, r.expires_at, r.ended_at, "
        "c.code, c.note, s.status, p.code, p.name "
        "FROM referral_redemptions r "
        "JOIN referral_codes c ON c.id = r.code_id "
        "JOIN subscriptions s ON s.id = r.subscription_id "
        "JOIN plans p ON p.id = s.plan_id "
        "WHERE r.user_id = %s ORDER BY r.redeemed_at DESC",
        (user_id,),
    )
    if not rows:
        return None
    (rid, rstatus, redeemed_at, expires_at, ended_at,
     code, note, sub_status, plan_code, plan_name) = rows[0]
    active = rstatus == "active" and sub_status == "active" and expires_at > now
    seconds_left = max(0, int((expires_at - now).total_seconds()))
    return {
        "redemption_id": rid,
        "code": code,
        "note": note,
        "plan": {"code": plan_code, "name": plan_name},
        "status": "active" if active else "expired",
        "started_at": iso(redeemed_at),
        "expires_at": iso(expires_at),
        "ended_at": None if active else iso(ended_at or expires_at),
        "days_left": math.ceil(seconds_left / 86400) if active else 0,
        "seconds_left": seconds_left if active else 0,
        "fallback": _fallback(),
    }


def run_expiry_sweep() -> dict:
    """Scheduler job: mark lapsed trials expired so the DB matches entitlement."""
    db.run(
        "UPDATE referral_redemptions SET status = 'expired', "
        "ended_at = COALESCE(ended_at, expires_at) "
        "WHERE status = 'active' AND expires_at <= SYSUTCDATETIME()"
    )
    db.run(
        "UPDATE s SET status = 'expired', cancel_at_period_end = 0, "
        "current_period_end = COALESCE(s.current_period_end, r.expires_at), "
        "updated_at = SYSUTCDATETIME() "
        "FROM subscriptions s "
        "JOIN referral_redemptions r ON r.subscription_id = s.id "
        "WHERE r.status = 'expired' AND s.is_referral = 1 AND s.status = 'active'"
    )
    active = db.get_single_value(
        "SELECT COUNT(*) FROM referral_redemptions WHERE status = 'active'")
    logger.info("Referral expiry sweep: %s trials still active", active)
    return {"active_trials": int(active or 0)}
