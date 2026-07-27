"""
Mock payment provider — for testing only.

create_payment_link  → returns a deterministic fake URL
verify_payment       → always returns True
handle_webhook       → echoes back paid=True
refund_payment       → always succeeds instantly (deterministic fake reference)
check_health         → always healthy (no real credentials to check)
"""

from backend.app.payments.base import PaymentProvider, ProviderHealthStatus, RefundResult


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

    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        order_number = getattr(order, "order_number", "TEST-000000")
        return RefundResult(success=True, status="COMPLETED", provider_reference=f"mock_refund_{order_number}")

    async def check_health(self, business) -> ProviderHealthStatus:
        return ProviderHealthStatus.now(healthy=True, provider="MOCK", detail="Test provider — always healthy")
