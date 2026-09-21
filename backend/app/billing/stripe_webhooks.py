"""
Stripe webhook handler — subscription status enforcement.
Automatically suspends/reactivates businesses based on payment status.

Signature verification (BUG-0001, now fixed): a missing/placeholder
STRIPE_WEBHOOK_SECRET or PAYFAST_BILLING_PASSPHRASE must NEVER mean "trust
the payload unverified" — that only happens in explicit ENVIRONMENT=
development/test, matching the pattern used by every other webhook in this
codebase (Meta, tenant Yoco/PayFast/Stitch/iKhoka). Each of these two routes
also only acts as a trusted endpoint for its OWN active, configured
provider — the inactive provider's billing route (and both, if billing is
disabled) reject with 404, so a stale/misconfigured route can't be used to
flip billing_status on a tenant.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from backend.app.core.config import _looks_like_placeholder, get_settings
from backend.app.db.session import get_db
from shared.models.billing_invoice import BillingInvoice
from shared.models.business import Business

logger = logging.getLogger("nextgen.billing")
router = APIRouter(prefix="/billing", tags=["billing"])
settings = get_settings()


async def _invoice_already_recorded(db: AsyncSession, provider: str, provider_reference: str | None) -> bool:
    """
    Application-level duplicate guard: a re-delivered webhook for an event
    already recorded as a BillingInvoice must be a full no-op (no second
    invoice row, no repeated billing_status mutation). No unique DB
    constraint exists on (provider, provider_reference) today — this is an
    existence check, not a race-proof constraint; a genuinely concurrent
    duplicate delivery is a real (low-probability) gap, tracked as a
    follow-up for a proper unique index (see KNOWN_BUGS.md).
    """
    if not provider_reference:
        return False
    result = await db.execute(
        select(BillingInvoice.id).where(
            BillingInvoice.provider == provider,
            BillingInvoice.provider_reference == provider_reference,
        )
    )
    return result.scalar_one_or_none() is not None


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
    if settings.BILLING_ENABLED and settings.BILLING_PROVIDER.upper() != "STRIPE":
        # This route only acts as a trusted endpoint for the ACTIVE billing
        # provider. Stripe isn't active — refuse rather than silently
        # accepting/ignoring, so a stale dashboard webhook config can't be
        # used against this deployment.
        raise HTTPException(status_code=404, detail="Not found")
    if not settings.BILLING_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.body()

    if settings.ENVIRONMENT in ("development", "test"):
        import json
        try:
            event = json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid payload")
    else:
        # An absent/placeholder secret must NEVER fall back to unverified
        # json.loads() — that was BUG-0001. Fail closed instead.
        if _looks_like_placeholder(settings.STRIPE_WEBHOOK_SECRET):
            logger.error("Stripe webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
            raise HTTPException(status_code=503, detail="Webhook not configured")
        if not stripe_signature:
            raise HTTPException(status_code=400, detail="Invalid signature")
        try:
            import stripe
            verified_event = stripe.Webhook.construct_event(
                body, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
            )
            # .to_dict() so downstream code (event.get(...)) works identically
            # regardless of which stripe-python major version is installed —
            # StripeObject's dict-vs-attribute behaviour has changed across
            # versions; converting once here avoids depending on that.
            event = verified_event.to_dict()
        except Exception as e:
            logger.warning("Stripe signature verification failed: %s", e)
            raise HTTPException(status_code=400, detail="Invalid signature")

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
    if settings.BILLING_ENABLED and settings.BILLING_PROVIDER.upper() != "PAYFAST":
        raise HTTPException(status_code=404, detail="Not found")
    if not settings.BILLING_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    raw_body = await request.body()
    import urllib.parse
    try:
        params = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid form data")

    from backend.app.payments.payfast import PayFastProvider, rand_amount_to_cents

    # PayFastProvider.verify_signature() fails closed on an empty/unset
    # passphrase (see payments/payfast.py) — no development bypass here, this
    # is NextGen's own money, not a tenant's, so there is no local-dev sender
    # for it either way.
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

    amount_cents = rand_amount_to_cents(params.get("amount_gross"))
    provider_reference = params.get("pf_payment_id")

    if await _invoice_already_recorded(db, "PAYFAST", provider_reference):
        logger.info("PayFast billing ITN: %s already recorded — skipping duplicate delivery", provider_reference)
        await db.commit()  # persist the subscription token update, if any
        return {"received": True}

    from backend.app.billing.lifecycle import mark_invoice_paid, mark_payment_failed
    if payment_status == "COMPLETE":
        mark_invoice_paid(biz, db=db, amount_cents=amount_cents, provider="PAYFAST", provider_reference=provider_reference)
    elif payment_status in ("FAILED", "CANCELLED"):
        # PayFast's ITN carries no reliable "when did this actually happen"
        # field distinct from delivery time, so the stale-event ordering
        # guard in mark_payment_failed (event_time) cannot be applied here —
        # a documented gap, not a silent one (see mark_payment_failed's
        # docstring and KNOWN_BUGS.md).
        mark_payment_failed(biz, db=db, amount_cents=amount_cents, provider="PAYFAST", provider_reference=provider_reference)

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

    provider_reference = data.get("id")
    if await _invoice_already_recorded(db, "STRIPE", provider_reference):
        logger.info("Stripe invoice.paid: %s already recorded — skipping duplicate delivery", provider_reference)
        return

    from backend.app.billing.lifecycle import mark_invoice_paid
    mark_invoice_paid(
        biz, db=db, amount_cents=data.get("amount_paid"),
        provider="STRIPE", provider_reference=provider_reference,
    )
    await db.commit()


async def _handle_payment_failed(db: AsyncSession, data: dict) -> None:
    biz = await _find_business_by_stripe(db, customer_id=data.get("customer"))
    if not biz:
        return

    provider_reference = data.get("id")
    if await _invoice_already_recorded(db, "STRIPE", provider_reference):
        logger.info("Stripe invoice.payment_failed: %s already recorded — skipping duplicate delivery", provider_reference)
        return

    # Stripe invoice objects carry `created` (unix epoch seconds) — used as
    # the ordering guard in mark_payment_failed so a stale, out-of-order
    # payment_failed delivery can't flip a business that has since paid back
    # to PAST_DUE.
    event_time = None
    created_ts = data.get("created")
    if isinstance(created_ts, (int, float)):
        from datetime import datetime, timezone
        event_time = datetime.fromtimestamp(created_ts, tz=timezone.utc)

    from backend.app.billing.lifecycle import mark_payment_failed
    mark_payment_failed(
        biz, db=db, amount_cents=data.get("amount_due"),
        provider="STRIPE", provider_reference=provider_reference,
        event_time=event_time,
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
