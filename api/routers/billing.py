# routers/billing.py
"""
Billing endpoints. The user identity ALWAYS comes from the access token;
no user id is accepted from the request body.

  GET  /billing/plans                 public catalog
  GET  /billing/me                    plan + usage + halt state (drives the whole UI)
  POST /billing/subscriptions         create a Razorpay subscription for checkout
  POST /billing/subscriptions/verify  verify checkout signature, grant access
  POST /billing/subscriptions/cancel  cancel at the end of the paid period
  POST /billing/webhook               Razorpay -> us (source of truth, signature-verified)
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from core import config, db
from core.security import get_current_user_id
from services import billing_service, razorpay_service, usage_service
from services.usage_service import iso

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(prefix="/billing", tags=["billing"])


# ───────────────────────────── Read endpoints ─────────────────────────────

@router.get("/plans")
def plans():
    return {"plans": [p.to_dict() for p in billing_service.list_plans()]}


@router.get("/me")
def me(user_id: int = Depends(get_current_user_id)):
    ent = billing_service.get_entitlement(user_id)
    snap = usage_service.snapshot(user_id)
    return {
        "plan": ent.plan.to_dict(),
        "subscription": None if ent.subscription_id is None else {
            "status": ent.status,
            "seats": ent.seats,
            "current_period_end": iso(ent.period_end),
            "cancel_at_period_end": ent.cancel_at_period_end,
            "is_owner": ent.is_owner,
        },
        "usage": {
            "session_tokens": snap.session_tokens,
            "session_resets_at": iso(snap.session_resets_at),
            "day_tokens": snap.day_tokens,
            "day_resets_at": iso(snap.day_resets_at),
            "month_queries": snap.month_queries,
            "month_resets_at": iso(snap.month_resets_at),
            "files_used": usage_service.files_used(user_id),
        },
        "halt": usage_service.compute_halt(ent.plan, snap),
    }


# ───────────────────────────── Checkout ─────────────────────────────

class CreateSubscriptionRequest(BaseModel):
    plan_code: str
    seats: int = Field(default=1, ge=1, le=1000)


@router.post("/subscriptions")
def create_subscription(payload: CreateSubscriptionRequest,
                        user_id: int = Depends(get_current_user_id)):
    plan = billing_service.get_plan_by_code(payload.plan_code)
    if plan is None or plan.code == "free":
        raise HTTPException(status_code=400, detail="Unknown or non-purchasable plan.")

    seats = payload.seats if plan.per_seat else 1
    if plan.code != "enterprise":
        seats = 1  # Individual / Maxx are single-user plans
    if seats < plan.min_seats:
        raise HTTPException(status_code=400,
                            detail=f"{plan.name} requires at least {plan.min_seats} seats.")

    rz_plan_id = config.RAZORPAY_PLAN_IDS.get(plan.code)
    if not rz_plan_id:
        logger.error("No Razorpay plan id configured for %s", plan.code)
        raise HTTPException(status_code=503, detail="Billing is not available for this plan yet.")

    current = billing_service.get_entitlement(user_id)
    if current.plan.code == plan.code and current.seats == seats and current.is_owner:
        raise HTTPException(status_code=409, detail="You are already on this plan.")

    try:
        remote = razorpay_service.create_subscription(
            rz_plan_id, seats, notes={"user_id": str(user_id), "plan_code": plan.code})
    except Exception:
        logger.exception("Razorpay subscription create failed")
        raise HTTPException(status_code=502, detail="Could not start checkout. Try again.")

    db.run(
        "INSERT INTO subscriptions (owner_user_id, plan_id, seats, status, razorpay_subscription_id) "
        "VALUES (%s, %s, %s, 'created', %s)",
        (user_id, plan.id, seats, remote["id"]),
    )
    # key_id is public; returning it means the client never hardcodes it.
    return {"subscription_id": remote["id"], "key_id": config.RAZORPAY_KEY_ID,
            "plan_code": plan.code, "seats": seats}


class VerifyRequest(BaseModel):
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


@router.post("/subscriptions/verify")
def verify_subscription(payload: VerifyRequest, user_id: int = Depends(get_current_user_id)):
    rows = db.fetch(
        "SELECT id FROM subscriptions WHERE razorpay_subscription_id = %s AND owner_user_id = %s",
        (payload.razorpay_subscription_id, user_id))
    if not rows:
        raise HTTPException(status_code=404, detail="Subscription not found.")
    local_id = rows[0][0]

    if not razorpay_service.verify_payment_signature(
            payload.razorpay_payment_id, payload.razorpay_subscription_id,
            payload.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment signature verification failed.")

    try:
        remote = razorpay_service.fetch_subscription(payload.razorpay_subscription_id)
    except Exception:
        logger.exception("Razorpay fetch failed during verify")
        remote = {}

    # A verified signature proves the first payment succeeded -> grant access now.
    # The webhook reconciles anything that differs.
    _sync_from_remote(local_id, remote, status_override="active")
    # If Razorpay's fetch failed we have no period yet: grant a provisional month until the webhook lands.
    db.run("UPDATE subscriptions SET current_period_end = DATEADD(day, 31, SYSUTCDATETIME()) "
           "WHERE id = %s AND current_period_end IS NULL", (local_id,))
    _activate(local_id)
    return me(user_id)


@router.post("/subscriptions/cancel")
def cancel_subscription(user_id: int = Depends(get_current_user_id)):
    rows = db.fetch(
        "SELECT TOP 1 id, razorpay_subscription_id FROM subscriptions "
        "WHERE owner_user_id = %s AND status = 'active' AND cancel_at_period_end = 0 "
        "ORDER BY created_at DESC", (user_id,))
    if not rows:
        raise HTTPException(status_code=400, detail="No active subscription to cancel.")
    local_id, rz_id = rows[0]
    try:
        razorpay_service.cancel_subscription(rz_id, at_cycle_end=True)
    except Exception:
        logger.exception("Razorpay cancel failed")
        raise HTTPException(status_code=502, detail="Could not cancel. Try again.")
    db.run("UPDATE subscriptions SET cancel_at_period_end = 1, updated_at = SYSUTCDATETIME() "
           "WHERE id = %s", (local_id,))
    return me(user_id)


# ───────────────────────────── Webhook ─────────────────────────────

@router.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-razorpay-signature", "")
    if not razorpay_service.verify_webhook_signature(raw, sig):
        raise HTTPException(status_code=400, detail="Invalid signature.")

    event = json.loads(raw)
    event_id = request.headers.get("x-razorpay-event-id") or ""
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event id.")

    await run_in_threadpool(_process_event, event_id, event, raw.decode("utf-8", "replace"))
    return {"status": "ok"}


def _process_event(event_id: str, event: dict, raw: str) -> None:
    if db.fetch("SELECT 1 FROM webhook_events WHERE event_id = %s", (event_id,)):
        return  # duplicate delivery
    etype = event.get("event", "")
    _handle_event(etype, event.get("payload", {}))
    # Recorded only AFTER successful handling: if handling raised, Razorpay gets a 500 and retries.
    db.run("INSERT INTO webhook_events (event_id, event_type, payload) VALUES (%s, %s, %s)",
           (event_id, etype, raw))


def _handle_event(etype: str, entities: dict) -> None:
    sub = (entities.get("subscription") or {}).get("entity")
    if not sub:
        return
    rows = db.fetch(
        "SELECT id, owner_user_id, status FROM subscriptions WHERE razorpay_subscription_id = %s",
        (sub["id"],))
    if not rows:
        logger.warning("Webhook for unknown subscription %s", sub["id"])
        return
    local_id, owner_id, local_status = rows[0]

    if etype in ("subscription.activated", "subscription.charged", "subscription.resumed"):
        if local_status in ("cancelled", "expired", "completed"):
            # Late event for a subscription we already ended (e.g. superseded by an upgrade).
            # Re-activating it would wrongly cancel the user's newer plan.
            logger.info("Ignoring %s for ended subscription %s", etype, local_id)
        else:
            _sync_from_remote(local_id, sub, status_override="active")
            _activate(local_id)
    elif etype in ("subscription.authenticated", "subscription.pending",
                   "subscription.halted", "subscription.paused",
                   "subscription.completed", "subscription.expired"):
        _sync_from_remote(local_id, sub)
    elif etype == "subscription.cancelled":
        _sync_from_remote(local_id, sub)
        db.run("UPDATE subscriptions SET cancelled_at = COALESCE(cancelled_at, SYSUTCDATETIME()) "
               "WHERE id = %s", (local_id,))

    pay = (entities.get("payment") or {}).get("entity")
    if etype == "subscription.charged" and pay:
        if not db.fetch("SELECT 1 FROM payments WHERE razorpay_payment_id = %s", (pay["id"],)):
            db.run(
                "INSERT INTO payments (user_id, subscription_id, razorpay_payment_id, "
                "razorpay_order_id, amount, currency, status, method) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'captured', %s)",
                (owner_id, local_id, pay["id"], pay.get("order_id"),
                 (pay.get("amount") or 0) / 100.0, pay.get("currency", "INR"),
                 pay.get("method")))


# ───────────────────────────── Helpers ─────────────────────────────

_ALLOWED_STATUS = {"created", "authenticated", "active", "pending", "halted",
                   "cancelled", "completed", "expired", "paused"}


def _sync_from_remote(local_id: int, remote: dict, status_override: Optional[str] = None) -> None:
    status = status_override or remote.get("status")
    if status not in _ALLOWED_STATUS:
        status = None  # unknown remote status: leave ours untouched
    start = razorpay_service.ts_to_dt(remote.get("current_start"))
    end = razorpay_service.ts_to_dt(remote.get("current_end"))
    db.run(
        "UPDATE subscriptions SET "
        "status = COALESCE(%s, status), "
        "current_period_start = COALESCE(%s, current_period_start), "
        "current_period_end = COALESCE(%s, current_period_end), "
        "razorpay_customer_id = COALESCE(%s, razorpay_customer_id), "
        "updated_at = SYSUTCDATETIME() WHERE id = %s",
        (status, start, end, remote.get("customer_id"), local_id),
    )


def _activate(local_id: int) -> None:
    """Idempotent: retire the owner's other paid subscriptions, ensure an org for Enterprise."""
    row = db.fetch(
        "SELECT s.owner_user_id, p.code FROM subscriptions s "
        "JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (local_id,))
    if not row:
        return
    owner_id, plan_code = row[0]

    others = db.fetch(
        "SELECT id, razorpay_subscription_id FROM subscriptions "
        "WHERE owner_user_id = %s AND id <> %s AND status = 'active'", (owner_id, local_id))
    for other_id, other_rz in others:
        try:
            razorpay_service.cancel_subscription(other_rz, at_cycle_end=False)
        except Exception:
            logger.exception("Could not cancel superseded subscription %s", other_rz)
        # NOTE: no proration/refund; the old plan ends immediately.
        db.run("UPDATE subscriptions SET status = 'cancelled', cancel_at_period_end = 0, "
               "cancelled_at = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME() WHERE id = %s",
               (other_id,))

    if plan_code == "enterprise":
        org = db.fetch("SELECT TOP 1 id FROM organizations WHERE owner_user_id = %s", (owner_id,))
        if org:
            org_id = org[0][0]
        else:
            name = db.get_single_value("SELECT name FROM auth WHERE id = %s", (owner_id,)) or "Team"
            org_id = db.insert(
                "INSERT INTO organizations (name, owner_user_id) OUTPUT INSERTED.id VALUES (%s, %s)",
                (f"{name}'s team", owner_id))
            db.run("INSERT INTO org_members (org_id, user_id, role, status) "
                   "VALUES (%s, %s, 'owner', 'active')", (org_id, owner_id))
        db.run("UPDATE subscriptions SET org_id = %s WHERE id = %s", (org_id, local_id))
