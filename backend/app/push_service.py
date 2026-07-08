"""
Web Push notification service.

Sends a push notification to every subscribed device for a given business.
Called fire-and-forget from order_creator after a new order is placed.

VAPID key setup (one-time, run on any machine with pywebpush installed):
    python -c "
    from py_vapid import Vapid
    import base64
    v = Vapid()
    v.generate_keys()
    print('VAPID_PRIVATE_KEY=' + base64.b64encode(v.private_pem()).decode())
    print('VAPID_PUBLIC_KEY=' + v.public_key.serialize().decode())
    "
Then add both values to .env.backend and to Render environment variables.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("nextgen.push")


async def send_order_alert(
    business_id: uuid.UUID,
    order_number: str,
    order_mode: str,
) -> None:
    """
    Background task: send push notifications to all staff subscriptions for a business.
    Opens its own DB session so this is fully independent of the order creation transaction.
    """
    from backend.app.core.config import get_settings
    settings = get_settings()
    if not settings.VAPID_PRIVATE_KEY or not settings.VAPID_PUBLIC_KEY:
        return

    try:
        pem_str = base64.b64decode(settings.VAPID_PRIVATE_KEY).decode("ascii")
    except Exception:
        logger.warning("VAPID_PRIVATE_KEY is not valid base64 — push skipped")
        return

    # Import session factory only after confirming push is configured, to avoid
    # triggering create_async_engine in test environments where the DB is not available.
    from backend.app.db.session import async_session_factory

    mode_label = "Pickup" if order_mode == "PICKUP" else "Delivery"
    payload = json.dumps({
        "title": "New Order!",
        "body": f"Order {order_number} — {mode_label}",
        "url": "/",
    })
    vapid_claims = {"sub": settings.VAPID_CONTACT_EMAIL}

    async with async_session_factory() as db:
        await _send_to_business(db, business_id, payload, pem_str, vapid_claims)


async def _send_to_business(
    db: AsyncSession,
    business_id: uuid.UUID,
    payload: str,
    pem_str: str,
    vapid_claims: dict,
) -> None:
    from shared.models.push_subscription import PushSubscription

    result = await db.execute(
        select(PushSubscription).where(PushSubscription.business_id == business_id)
    )
    subscriptions = result.scalars().all()
    if not subscriptions:
        return

    dead_endpoints: list[str] = []

    for sub in subscriptions:
        try:
            await asyncio.to_thread(
                _send_one,
                sub.endpoint,
                sub.p256dh,
                sub.auth,
                payload,
                pem_str,
                vapid_claims,
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                dead_endpoints.append(sub.endpoint)
            else:
                logger.warning("Push failed for endpoint …%s: %s", sub.endpoint[-12:], exc)

    if dead_endpoints:
        from shared.models.push_subscription import PushSubscription
        for endpoint in dead_endpoints:
            await db.execute(
                delete(PushSubscription).where(
                    PushSubscription.business_id == business_id,
                    PushSubscription.endpoint == endpoint,
                )
            )
        await db.commit()


def _send_one(
    endpoint: str,
    p256dh: str,
    auth: str,
    payload: str,
    pem_str: str,
    vapid_claims: dict,
) -> None:
    from pywebpush import webpush, WebPushException  # type: ignore[import]
    webpush(
        subscription_info={
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
        },
        data=payload,
        vapid_private_key=pem_str,
        vapid_claims=vapid_claims,
    )
