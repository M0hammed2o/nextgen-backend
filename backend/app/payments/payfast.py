"""
PayFast payment provider — placeholder.

Real integration requires PAYFAST_MERCHANT_ID + PAYFAST_MERCHANT_KEY.
"""

from backend.app.payments.base import PaymentProvider


class PayFastProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        raise NotImplementedError("PayFast integration not yet configured.")

    async def verify_payment(self, order, business) -> bool:
        raise NotImplementedError("PayFast integration not yet configured.")

    async def handle_webhook(self, payload: dict) -> dict:
        raise NotImplementedError("PayFast integration not yet configured.")
