"""
Direct EFT payment provider.

No external API — the "payment link" is the EFT instructions themselves.
create_payment_link returns None because EFT details are in the message.
verify_payment always returns False (manual confirmation required).
handle_webhook is not applicable.
"""

from backend.app.payments.base import PaymentProvider


class DirectEFTProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        return None  # EFT details are included in the WhatsApp message

    async def verify_payment(self, order, business) -> bool:
        return False  # Manual: staff marks as paid

    async def handle_webhook(self, payload: dict) -> dict:
        return {"order_id": "", "paid": False, "reference": None}
