"""
Yoco payment provider — placeholder.

Real integration requires YOCO_SECRET_KEY from environment.
"""

from backend.app.payments.base import PaymentProvider


class YocoProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        raise NotImplementedError("Yoco integration not yet configured. Add YOCO_SECRET_KEY.")

    async def verify_payment(self, order, business) -> bool:
        raise NotImplementedError("Yoco integration not yet configured.")

    async def handle_webhook(self, payload: dict) -> dict:
        raise NotImplementedError("Yoco integration not yet configured.")
