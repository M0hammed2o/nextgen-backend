"""
Direct EFT payment provider.

No external API — the "payment link" is the EFT instructions themselves.
create_payment_link returns None because EFT details are in the message.
verify_payment always returns False (manual confirmation required).
handle_webhook is not applicable.
refund_payment always returns MANUAL_REQUIRED — there is no API to call;
the business must move the money back themselves, same as the original
payment was manual. check_health is always healthy — there's no external
dependency or credential to misconfigure for this provider.
"""

from backend.app.payments.base import PaymentProvider, ProviderHealthStatus, RefundResult


class DirectEFTProvider(PaymentProvider):
    async def create_payment_link(self, order, business) -> str | None:
        return None  # EFT details are included in the WhatsApp message

    async def verify_payment(self, order, business) -> bool:
        return False  # Manual: staff marks as paid

    async def handle_webhook(self, payload: dict) -> dict:
        return {"order_id": "", "paid": False, "reference": None}

    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        return RefundResult(
            success=False,
            status="MANUAL_REQUIRED",
            error="Direct EFT has no refund API — transfer the refund back to the customer's bank account manually.",
        )

    async def check_health(self, business) -> ProviderHealthStatus:
        return ProviderHealthStatus.now(healthy=True, provider="DIRECT_EFT", detail="No external dependency")
