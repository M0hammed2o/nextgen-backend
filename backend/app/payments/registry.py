"""
Payment provider registry — maps provider names to provider instances.

Usage:
    provider = get_provider("YOCO", business)
    link = await provider.create_payment_link(order, business)
"""

from backend.app.payments.base import PaymentProvider
from backend.app.payments.direct_eft import DirectEFTProvider
from backend.app.payments.mock_provider import MockPaymentProvider
from backend.app.payments.yoco import YocoProvider
from backend.app.payments.payfast import PayFastProvider
from backend.app.payments.stitch import StitchProvider

_PROVIDERS: dict[str, PaymentProvider] = {
    "DIRECT_EFT": DirectEFTProvider(),
    "MOCK": MockPaymentProvider(),
    "YOCO": YocoProvider(),
    "PAYFAST": PayFastProvider(),
    "STITCH": StitchProvider(),
}


def get_provider(provider_name: str | None, business=None) -> PaymentProvider | None:
    """
    Return the provider instance for the given name, or None if unknown.

    Args:
        provider_name: e.g. "YOCO", "DIRECT_EFT", "MOCK"
        business: unused today, reserved for per-business credential lookup
    """
    if not provider_name:
        return None
    return _PROVIDERS.get(provider_name.upper())
