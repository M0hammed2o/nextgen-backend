"""
BillingProvider registry — selects the active SaaS-billing provider from
config (`settings.BILLING_PROVIDER`). Adding a provider means implementing
`BillingProvider` and registering it here; nothing else in the codebase
needs to change (same pattern as `backend/app/payments/registry.py`).
"""

from backend.app.billing.payfast_billing import PayFastBillingProvider
from backend.app.billing.provider_base import BillingProvider
from backend.app.billing.stripe_billing import StripeBillingProvider
from backend.app.core.config import get_settings

_PROVIDERS: dict[str, BillingProvider] = {
    "STRIPE": StripeBillingProvider(),
    "PAYFAST": PayFastBillingProvider(),
}


def get_billing_provider() -> BillingProvider:
    settings = get_settings()
    provider = _PROVIDERS.get(settings.BILLING_PROVIDER.upper())
    if not provider:
        raise ValueError(
            f"Unknown BILLING_PROVIDER={settings.BILLING_PROVIDER!r} — "
            f"must be one of {sorted(_PROVIDERS)}"
        )
    return provider
