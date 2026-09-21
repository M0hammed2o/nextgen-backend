"""
Web Push subscription management routes.

GET  /v1/push/vapid-public-key   — returns the VAPID public key (no auth)
POST /v1/push/subscribe          — register a browser push subscription
DELETE /v1/push/unsubscribe      — remove a browser push subscription
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.rbac import AuthUser, require_staff_or_above
from backend.app.db.session import get_db
from shared.models.push_subscription import PushSubscription

router = APIRouter(prefix="/push", tags=["push"])


class SubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class UnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/vapid-public-key")
async def get_vapid_public_key():
    """Return the VAPID public key so the browser can subscribe to push."""
    settings = get_settings()
    if not settings.VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Push notifications not configured")
    return {"vapid_public_key": settings.VAPID_PUBLIC_KEY}


@router.post("/subscribe", status_code=201)
async def subscribe(
    body: SubscribeRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Register (or refresh) a browser push subscription for the current staff member."""
    if not user.business_id:
        raise HTTPException(status_code=403, detail="No business context")

    # `endpoint` is globally unique (uq_push_subscription_endpoint) — a real
    # browser subscription can legitimately move between staff on the same
    # shared till device, and can legitimately move between businesses if
    # the same physical device is reused at another restaurant. But it must
    # never let a caller who merely knows/guesses a victim's endpoint URL
    # re-point that victim's subscription to themselves.
    #
    # Rule: same-business re-assignment is always allowed (ordinary staff
    # handover on a shared device). Cross-business re-assignment is only
    # allowed when the presented p256dh/auth keys match what's already
    # stored — those keys come from the browser's own subscription object,
    # so a match is proof this request really originated from the same
    # browser that owns the subscription, not just knowledge of the URL.
    # A cross-business request with mismatched keys leaves the other
    # tenant's row untouched and gets the same success-shaped response, so
    # it learns nothing about whether that row exists.
    result = await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    sub = result.scalar_one_or_none()

    if sub and sub.business_id != user.business_id:
        if sub.p256dh == body.p256dh and sub.auth == body.auth:
            sub.business_id = user.business_id
            sub.user_id = user.user_id
        # else: mismatched keys — leave the other tenant's row alone and
        # fall through to the same response as a normal success.
    elif sub:
        sub.p256dh = body.p256dh
        sub.auth = body.auth
        sub.user_id = user.user_id
    else:
        sub = PushSubscription(
            id=uuid.uuid4(),
            business_id=user.business_id,
            user_id=user.user_id,
            endpoint=body.endpoint,
            p256dh=body.p256dh,
            auth=body.auth,
        )
        db.add(sub)

    await db.commit()
    return {"subscribed": True}


@router.delete("/unsubscribe", status_code=200)
async def unsubscribe(
    body: UnsubscribeRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Remove a push subscription (e.g. when staff logs out)."""
    if not user.business_id:
        raise HTTPException(status_code=403, detail="No business context")

    await db.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == body.endpoint,
            PushSubscription.business_id == user.business_id,
        )
    )
    await db.commit()
    return {"unsubscribed": True}
