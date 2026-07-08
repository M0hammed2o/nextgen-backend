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

    # Upsert: if the endpoint already exists for this user, update keys.
    result = await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    sub = result.scalar_one_or_none()

    if sub:
        sub.p256dh = body.p256dh
        sub.auth = body.auth
        sub.business_id = user.business_id
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
