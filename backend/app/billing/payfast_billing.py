"""
PayFastBillingProvider — NextGen's own SaaS-billing option using PayFast's
recurring-billing ("Subscriptions") product, under NextGen's own merchant
account (PAYFAST_BILLING_* settings — distinct from each tenant's own
Business.payment_api_key/etc., which are for the tenant's *customer*
payments via backend/app/payments/payfast.py).

UNVERIFIED against live credentials or a sandbox account — implemented
against PayFast's documented recurring-billing checkout parameters and
their subscriptions-management REST API, reusing the exact signature
helper (`_build_signature`) already proven correct for the customer-payment
PayFast integration. See KNOWN_BUGS.md for what needs live confirmation
before this is relied on in production.

Deliberately has no self-service portal (PayFast has no Stripe-Billing-
Portal equivalent) — `create_portal_session` always returns None and
`supports_self_service_portal()` is False, so callers degrade gracefully
(e.g. "contact support to change your plan") rather than showing a broken link.
"""

import logging
import urllib.parse

import httpx

from backend.app.billing.provider_base import BillingCancelResult, BillingProvider
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from backend.app.payments.payfast import _build_signature

logger = logging.getLogger("nextgen.billing.payfast")
settings = get_settings()

_CHECKOUT_URL = "https://www.payfast.co.za/eng/process"
_SUBSCRIPTION_API_URL = "https://api.payfast.co.za/subscriptions/{token}/cancel"

# One Stripe Price ID's worth of info, but PayFast has no product/price
# catalogue API — the Rand amount per plan is just a plain constant here.
_PLAN_MONTHLY_AMOUNT_RANDS = {
    "STARTER": "1200.00",
    "GROWTH": "1200.00",  # NextGen Restaurant is a single flat-rate plan today; both map to the same price
}


class PayFastBillingProvider(BillingProvider):

    async def create_checkout_session(self, db, business, plan: str) -> str:
        if not settings.PAYFAST_BILLING_MERCHANT_ID or not settings.PAYFAST_BILLING_MERCHANT_KEY:
            raise AppError("BILLING_PROVIDER_NOT_CONFIGURED", "PayFast SaaS billing is not configured", 500)

        amount = _PLAN_MONTHLY_AMOUNT_RANDS.get(plan.upper())
        if not amount:
            raise AppError("PLAN_NOT_SELF_SERVICE", f"Plan '{plan}' is not available for self-service checkout", 422)

        params: dict[str, str] = {
            "merchant_id": settings.PAYFAST_BILLING_MERCHANT_ID,
            "merchant_key": settings.PAYFAST_BILLING_MERCHANT_KEY,
            "return_url": settings.BILLING_SUCCESS_URL,
            "cancel_url": settings.BILLING_CANCEL_URL,
            "notify_url": f"{settings.BACKEND_PUBLIC_URL}/v1/billing/webhooks/payfast",
            "m_payment_id": str(business.id),
            "amount": amount,
            "item_name": f"NextGen Restaurant subscription ({plan.upper()})",
            # Recurring-billing params — PayFast's "Subscriptions" product.
            "subscription_type": "1",
            "recurring_amount": amount,
            "frequency": "3",  # monthly
            "cycles": "0",     # indefinite, until cancelled
        }
        params["signature"] = _build_signature(params, settings.PAYFAST_BILLING_PASSPHRASE)

        url = f"{_CHECKOUT_URL}?{urllib.parse.urlencode(params)}"
        logger.info("PayFast billing checkout built for business %s", business.id)
        return url

    async def create_portal_session(self, business) -> str | None:
        return None

    async def cancel_subscription(self, db, business) -> BillingCancelResult:
        if not business.payfast_subscription_token:
            return BillingCancelResult(success=False, status="FAILED", error="No active PayFast subscription token on this business")

        params = {"merchant-id": settings.PAYFAST_BILLING_MERCHANT_ID, "token": business.payfast_subscription_token}
        signature = _build_signature(params, settings.PAYFAST_BILLING_PASSPHRASE)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.put(
                    _SUBSCRIPTION_API_URL.format(token=business.payfast_subscription_token),
                    headers={
                        "merchant-id": settings.PAYFAST_BILLING_MERCHANT_ID,
                        "signature": signature,
                    },
                )
                resp.raise_for_status()
                return BillingCancelResult(success=True, status="COMPLETED")
        except httpx.HTTPStatusError as exc:
            logger.error("PayFast subscription cancel %s error for business %s: %s", exc.response.status_code, business.id, exc.response.text)
            return BillingCancelResult(success=False, status="FAILED", error=f"PayFast API error: {exc.response.status_code}")
        except Exception as exc:
            logger.exception("PayFast subscription cancel failed for business %s", business.id)
            return BillingCancelResult(success=False, status="FAILED", error=str(exc))

    def supports_self_service_portal(self) -> bool:
        return False
