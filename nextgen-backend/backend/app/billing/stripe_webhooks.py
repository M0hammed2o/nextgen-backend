"""
Stripe webhook handler — subscription status enforcement.
Automatically suspends/reactivates businesses based on payment status.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from backend.app.core.config import get_settings
from backend.app.db.session import get_db
from shared.models.business import Business

logger = logging.getLogger("nextgen.billing")
router = APIRouter(prefix="/billing", tags=["billing"])
settings = get_settings()


@router.post("/webhook", status_code=200)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str | None = Header(None, alias="Stripe-Signature"),
):
    """
    Handle Stripe webhook events for subscription management.
    
    Handled events:
    - customer.subscription.updated → update billing_status
    - invoice.paid → reactivate if was past_due
    - invoice.payment_failed → mark as PAST_DUE, eventually suspend
    - customer.subscription.deleted → suspend business
    """
    body = await request.body()

    # Verify signature in production
    if settings.ENVIRONMENT != "development" and settings.STRIPE_WEBHOOK_SECRET:
        try:
            import stripe
            event = stripe.Webhook.construct_event(
                body, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
            )
        except Exception as e:
            logger.warning("Stripe signature verification failed: %s", e)
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        import json
        event = json.loads(body)

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    logger.info("Stripe webhook received: %s", event_type)

    if event_type == "customer.subscription.updated":
        await _handle_subscription_updated(db, data)

    elif event_type == "invoice.paid":
        await _handle_invoice_paid(db, data)

    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(db, data)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(db, data)

    return {"status": "ok"}


async def _find_business_by_stripe(
    db: AsyncSession, customer_id: str | None = None, subscription_id: str | None = None
) -> Business | None:
    """Find a business by Stripe customer ID or subscription ID."""
    if subscription_id:
        result = await db.execute(
            select(Business).where(Business.stripe_subscription_id == subscription_id)
        )
        biz = result.scalar_one_or_none()
        if biz:
            return biz

    if customer_id:
        result = await db.execute(
            select(Business).where(Business.stripe_customer_id == customer_id)
        )
        return result.scalar_one_or_none()

    return None


async def _handle_subscription_updated(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(
        db,
        customer_id=data.get("customer"),
        subscription_id=data.get("id"),
    )
    if not biz:
        logger.warning("No business found for subscription %s", data.get("id"))
        return

    status = data.get("status", "")
    status_map = {
        "active": "ACTIVE",
        "past_due": "PAST_DUE",
        "canceled": "CANCELLED",
        "unpaid": "SUSPENDED",
        "trialing": "TRIAL",
    }

    new_status = status_map.get(status)
    if new_status:
        biz.billing_status = new_status
        if new_status == "SUSPENDED":
            biz.is_active = False
            biz.suspended_reason = "Subscription unpaid"
        elif new_status == "ACTIVE":
            biz.is_active = True
            biz.suspended_reason = None

    await db.commit()
    logger.info("Business %s billing status updated to %s", biz.id, new_status)


async def _handle_invoice_paid(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(db, customer_id=data.get("customer"))
    if not biz:
        return

    if biz.billing_status == "PAST_DUE":
        biz.billing_status = "ACTIVE"
        biz.is_active = True
        biz.suspended_reason = None
        await db.commit()
        logger.info("Business %s reactivated after payment", biz.id)


async def _handle_payment_failed(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(db, customer_id=data.get("customer"))
    if not biz:
        return

    biz.billing_status = "PAST_DUE"
    await db.commit()
    logger.info("Business %s marked as PAST_DUE after payment failure", biz.id)


async def _handle_subscription_deleted(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(
        db, subscription_id=data.get("id"), customer_id=data.get("customer")
    )
    if not biz:
        return

    biz.billing_status = "CANCELLED"
    biz.is_active = False
    biz.suspended_reason = "Subscription cancelled"
    biz.stripe_subscription_id = None
    await db.commit()
    logger.info("Business %s suspended: subscription cancelled", biz.id)
