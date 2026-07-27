"""
Stripe client helpers for NextGen's own SaaS subscription billing of tenants.

Distinct from `backend/app/payments/*` (which lets a *tenant's own customers*
pay the tenant via Yoco/PayFast/Stitch/iKhoka) — this module is exclusively
about NextGen charging the *tenant* for platform access.

Kept as plain functions, not a class, so tests can monkeypatch each one
individually without needing a fake Stripe SDK.
"""

import logging

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from shared.models.business import Business

logger = logging.getLogger("nextgen.billing")
settings = get_settings()

# Self-service Checkout only exists for plans with a configured Price ID.
# ENTERPRISE is deliberately excluded — sales-assisted/custom-contract only,
# per PRODUCT_ROADMAP.md's packaging section.
_PLAN_PRICE_IDS = {
    "STARTER": settings.STRIPE_PRICE_ID_STARTER,
    "GROWTH": settings.STRIPE_PRICE_ID_GROWTH,
}


def get_price_id_for_plan(plan: str) -> str:
    """Return the configured Stripe Price ID for a self-service plan, or raise."""
    price_id = _PLAN_PRICE_IDS.get(plan.upper())
    if not price_id:
        raise AppError(
            "PLAN_NOT_SELF_SERVICE",
            f"Plan '{plan}' is not available for self-service checkout — contact sales",
            422,
        )
    return price_id


async def ensure_stripe_customer(db, business: Business) -> str:
    """
    Return this business's Stripe Customer ID, creating one if it doesn't
    exist yet. Persists the new ID on the business row.
    """
    if business.stripe_customer_id:
        return business.stripe_customer_id

    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY

    customer = stripe.Customer.create(
        name=business.name,
        metadata={"business_id": str(business.id)},
    )
    business.stripe_customer_id = customer["id"]
    await db.commit()
    logger.info("Created Stripe customer %s for business %s", customer["id"], business.id)
    return customer["id"]


async def create_checkout_session_url(db, business: Business, plan: str) -> str:
    """
    Create a Stripe Checkout Session (subscription mode) for the given plan
    and return its hosted URL. The business is redirected there to enter
    payment details — NextGen never handles card data directly.
    """
    price_id = get_price_id_for_plan(plan)
    customer_id = await ensure_stripe_customer(db, business)

    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=settings.BILLING_SUCCESS_URL,
        cancel_url=settings.BILLING_CANCEL_URL,
        metadata={"business_id": str(business.id), "plan": plan.upper()},
    )
    return session["url"]


async def create_portal_session_url(business: Business) -> str:
    """
    Create a Stripe Billing Portal session so the owner can manage/upgrade/
    cancel their own subscription without contacting support. Requires an
    existing Stripe customer — i.e. at least one checkout must have happened.
    """
    if not business.stripe_customer_id:
        raise AppError(
            "NO_STRIPE_CUSTOMER",
            "No billing account exists yet — complete checkout first",
            422,
        )

    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY

    session = stripe.billing_portal.Session.create(
        customer=business.stripe_customer_id,
        return_url=settings.BILLING_PORTAL_RETURN_URL,
    )
    return session["url"]
