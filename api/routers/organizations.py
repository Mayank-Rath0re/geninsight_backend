# routers/organizations.py
"""
Enterprise seat management. An org is created automatically when its owner's Enterprise
subscription activates (routers/billing.py). Members share the owner's plan.

  GET  /org/me              org, members, seat usage (404-free: {"org": null} if none)
  POST /org/members         owner adds an existing, verified user by email
  POST /org/members/remove  owner removes a member
  POST /org/leave           a member leaves

Adding requires the person to have signed up already (no email-invite flow yet).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from core import db
from core.security import get_current_user_id
from services import billing_service

logger = logging.getLogger("my_global_app_logger")
router = APIRouter(prefix="/org", tags=["organization"])


def _membership(user_id: int):
    rows = db.fetch(
        "SELECT o.id, o.name, o.owner_user_id, m.role FROM org_members m "
        "JOIN organizations o ON o.id = m.org_id "
        "WHERE m.user_id = %s AND m.status = 'active' ORDER BY o.id",
        (user_id,),
    )
    return rows[0] if rows else None


def _seats_for(owner_id: int, org_id: int) -> int:
    ent = billing_service.get_entitlement(owner_id)
    return ent.seats if (ent.plan.code == "enterprise" and ent.org_id == org_id) else 0


def _active_count(org_id: int) -> int:
    return int(db.get_single_value(
        "SELECT COUNT(*) FROM org_members WHERE org_id = %s AND status = 'active'", (org_id,)) or 0)


def _require_owner(user_id: int):
    m = _membership(user_id)
    if not m or m[2] != user_id:
        raise HTTPException(status_code=403, detail="Only the organization owner can do this.")
    return m


@router.get("/me")
def my_org(user_id: int = Depends(get_current_user_id)):
    m = _membership(user_id)
    if not m:
        return {"org": None}
    org_id, name, owner_id, role = m
    members = db.fetch(
        "SELECT a.id, a.email, a.name, m.role FROM org_members m "
        "JOIN auth a ON a.id = m.user_id "
        "WHERE m.org_id = %s AND m.status = 'active' ORDER BY m.created_at",
        (org_id,),
    )
    return {
        "org": {
            "id": org_id, "name": name, "role": role,
            "seats": _seats_for(owner_id, org_id),
            "seats_used": len(members),
            "members": [{"user_id": r[0], "email": r[1], "name": r[2], "role": r[3]} for r in members],
        }
    }


class AddMemberRequest(BaseModel):
    email: EmailStr


@router.post("/members")
def add_member(payload: AddMemberRequest, user_id: int = Depends(get_current_user_id)):
    org_id, _name, owner_id, _role = _require_owner(user_id)
    seats = _seats_for(owner_id, org_id)
    if seats == 0:
        raise HTTPException(status_code=403, detail="Your Enterprise subscription is not active.")

    target = db.fetch(
        "SELECT id FROM auth WHERE email = %s AND is_verified = 1 AND is_deleted = 0", (payload.email,))
    if not target:
        raise HTTPException(status_code=404, detail="No verified account with that email. Ask them to sign up first.")
    target_id = target[0][0]

    existing = db.fetch(
        "SELECT status FROM org_members WHERE org_id = %s AND user_id = %s", (org_id, target_id))
    if existing and existing[0][0] == "active":
        raise HTTPException(status_code=409, detail="Already a member.")
    if _active_count(org_id) >= seats:
        raise HTTPException(status_code=403,
                            detail={"code": "SEAT_LIMIT", "message": f"All {seats} seats are in use."})

    if existing:
        db.run("UPDATE org_members SET status = 'active', role = 'member' WHERE org_id = %s AND user_id = %s",
               (org_id, target_id))
    else:
        db.run("INSERT INTO org_members (org_id, user_id, role, status, invited_email) "
               "VALUES (%s, %s, 'member', 'active', %s)", (org_id, target_id, payload.email))
    return my_org(user_id)


class RemoveMemberRequest(BaseModel):
    user_id: int


@router.post("/members/remove")
def remove_member(payload: RemoveMemberRequest, user_id: int = Depends(get_current_user_id)):
    org_id, _name, owner_id, _role = _require_owner(user_id)
    if payload.user_id == owner_id:
        raise HTTPException(status_code=400, detail="The owner cannot be removed.")
    db.run("UPDATE org_members SET status = 'removed' WHERE org_id = %s AND user_id = %s AND role = 'member'",
           (org_id, payload.user_id))
    return my_org(user_id)


@router.post("/leave")
def leave_org(user_id: int = Depends(get_current_user_id)):
    m = _membership(user_id)
    if not m:
        raise HTTPException(status_code=400, detail="You are not in an organization.")
    if m[2] == user_id:
        raise HTTPException(status_code=400, detail="The owner cannot leave; cancel the subscription instead.")
    db.run("UPDATE org_members SET status = 'removed' WHERE org_id = %s AND user_id = %s", (m[0], user_id))
    return {"org": None}
