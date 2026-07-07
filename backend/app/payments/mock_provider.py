"""
Mock payment provider — for testing only.

create_payment_link  → returns a deterministic fake URL
verify_payment       → always returns True
handle_webhook       → echoes back paid=True
"""

from backend.app.payments.base import PaymentProvider


class MockPaymentProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        order_number = getattr(order, "order_number", "TEST-000000")
        return f"https://pay.mock.test/checkout/{order_number}"

    async def verify_payment(self, order, business) -> bool:
        return True

    async def handle_webhook(self, payload: dict) -> dict:
        return {
            "order_id": payload.get("order_id", ""),
            "paid": True,
            "reference": payload.get("reference"),
        }
