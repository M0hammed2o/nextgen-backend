"""
Stitch payment provider — placeholder.

Real integration requires STITCH_CLIENT_ID + STITCH_CLIENT_SECRET.
"""

from backend.app.payments.base import PaymentProvider


class StitchProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        raise NotImplementedError("Stitch integration not yet configured.")

    async def verify_payment(self, order, business) -> bool:
        raise NotImplementedError("Stitch integration not yet configured.")

    async def handle_webhook(self, payload: dict) -> dict:
        raise NotImplementedError("Stitch integration not yet configured.")
