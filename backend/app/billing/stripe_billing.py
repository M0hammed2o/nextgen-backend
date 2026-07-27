"""
StripeBillingProvider — wraps the existing stripe_client.py functions
(unchanged, still directly importable) behind the BillingProvider interface,
so routes_billing.py can go through one provider-agnostic entry point
(backend/app/billing/registry.py::get_billing_provider) instead of importing
Stripe-specific functions directly.
"""

import logging

from backend.app.billing.provider_base import BillingCancelResult, BillingProvider
from backend.app.billing.stripe_client import create_checkout_session_url, create_portal_session_url
from backend.app.core.config import get_settings

logger = logging.getLogger("nextgen.billing.stripe")
settings = get_settings()


class StripeBillingProvider(BillingProvider):

    async def create_checkout_session(self, db, business, plan: str) -> str:
        return await create_checkout_session_url(db, business, plan)

    async def create_portal_session(self, business) -> str | None:
        return await create_portal_session_url(business)

    async def cancel_subscription(self, db, business) -> BillingCancelResult:
        if not business.stripe_subscription_id:
            return BillingCancelResult(success=False, status="FAILED", error="No active Stripe subscription on this business")

        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe.Subscription.cancel(business.stripe_subscription_id)
            return BillingCancelResult(success=True, status="COMPLETED")
        except Exception as exc:
            logger.exception("Stripe subscription cancel failed for business %s", business.id)
            return BillingCancelResult(success=False, status="FAILED", error=str(exc))

    def supports_self_service_portal(self) -> bool:
        return True
