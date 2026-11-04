# services/razorpay_service.py
"""Thin Razorpay wrapper. `pip install razorpay`."""

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional

import razorpay

from core import config

_client: Optional[razorpay.Client] = None

# Razorpay subscription billing cycles to authorise up front (monthly plan => ~10 years).
TOTAL_COUNT = 120


def client() -> razorpay.Client:
    global _client
    if _client is None:
        if not config.RAZORPAY_KEY_ID or not config.RAZORPAY_KEY_SECRET:
            raise RuntimeError("Razorpay keys are not configured.")
        _client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
    return _client


def create_subscription(rz_plan_id: str, quantity: int, notes: dict) -> dict:
    return client().subscription.create({
        "plan_id": rz_plan_id,
        "total_count": TOTAL_COUNT,
        "quantity": quantity,
        "customer_notify": 1,
        "notes": notes,
    })


def fetch_subscription(rz_subscription_id: str) -> dict:
    return client().subscription.fetch(rz_subscription_id)


def cancel_subscription(rz_subscription_id: str, at_cycle_end: bool) -> dict:
    return client().subscription.cancel(
        rz_subscription_id, {"cancel_at_cycle_end": 1 if at_cycle_end else 0})


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_payment_signature(payment_id: str, subscription_id: str, signature: str) -> bool:
    expected = _hmac(config.RAZORPAY_KEY_SECRET, f"{payment_id}|{subscription_id}".encode())
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    if not config.RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = _hmac(config.RAZORPAY_WEBHOOK_SECRET, raw_body)
    return hmac.compare_digest(expected, signature or "")


def ts_to_dt(ts) -> Optional[datetime]:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None) if ts else None
