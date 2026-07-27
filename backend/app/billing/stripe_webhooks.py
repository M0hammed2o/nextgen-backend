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


@router.post("/webhooks/payfast", status_code=200, include_in_schema=False)
async def payfast_billing_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive PayFast ITN for NextGen's OWN SaaS-billing merchant account
    (PayFastBillingProvider) — distinct from
    `routes_payments.py::payfast_webhook`, which is per-*tenant* PayFast
    (each business's own customer payments). Verifies with the platform's
    own PAYFAST_BILLING_PASSPHRASE, never a tenant's payment_webhook_secret.
    """
    raw_body = await request.body()
    import urllib.parse
    try:
        params = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid form data")

    from backend.app.payments.payfast import PayFastProvider
    params_copy = dict(params)
    if not PayFastProvider.verify_signature(params_copy, settings.PAYFAST_BILLING_PASSPHRASE):
        logger.warning("PayFast billing ITN: invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    business_id = params.get("m_payment_id")
    if not business_id:
        return {"received": True}

    result = await db.execute(select(Business).where(Business.id == business_id))
    biz = result.scalar_one_or_none()
    if not biz:
        logger.warning("PayFast billing ITN: unknown business %s", business_id)
        return {"received": True}

    payment_status = params.get("payment_status", "")
    token = params.get("token")
    if token:
        biz.payfast_subscription_token = token

    amount_cents = None
    if params.get("amount_gross"):
        try:
            amount_cents = round(float(params["amount_gross"]) * 100)
        except ValueError:
            amount_cents = None

    from backend.app.billing.lifecycle import mark_invoice_paid, mark_payment_failed
    if payment_status == "COMPLETE":
        mark_invoice_paid(biz, db=db, amount_cents=amount_cents, provider="PAYFAST", provider_reference=params.get("pf_payment_id"))
    elif payment_status in ("FAILED", "CANCELLED"):
        mark_payment_failed(biz, db=db, amount_cents=amount_cents, provider="PAYFAST", provider_reference=params.get("pf_payment_id"))

    await db.commit()
    return {"received": True}


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

    from backend.app.billing.lifecycle import mark_invoice_paid
    mark_invoice_paid(
        biz, db=db, amount_cents=data.get("amount_paid"),
        provider="STRIPE", provider_reference=data.get("id"),
    )
    await db.commit()


async def _handle_payment_failed(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(db, customer_id=data.get("customer"))
    if not biz:
        return

    from backend.app.billing.lifecycle import mark_payment_failed
    mark_payment_failed(
        biz, db=db, amount_cents=data.get("amount_due"),
        provider="STRIPE", provider_reference=data.get("id"),
    )
    await db.commit()


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
