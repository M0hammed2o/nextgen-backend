"""
Mock payment provider — for testing only.

create_payment_link  → returns a deterministic fake URL (order-bound) or a
                        fake URL built from the caller-supplied reference
                        (order=None, the POS Pay-by-Link intent path)
verify_payment       → always returns True
handle_webhook       → echoes back paid=True, plus amount_cents/currency if
                        given in the payload (so tests can exercise the real
                        amount-verification logic in _mark_order_paid /
                        _mark_intent_paid against the mock, not just against
                        real providers)
refund_payment       → always succeeds instantly (deterministic fake reference)
check_health         → always healthy (no real credentials to check)
"""

from backend.app.payments.base import (
    PaymentProvider,
    ProviderHealthStatus,
    RefundResult,
    resolve_amount_and_reference,
)


class MockPaymentProvider(PaymentProvider):
    async def create_payment_link(
        self,
        order,
        business,
        *,
        amount_cents: int | None = None,
        external_reference: str | None = None,
    ) -> str | None:
        _, resolved_reference = resolve_amount_and_reference(order, amount_cents, external_reference)
        if order is not None:
            order_number = getattr(order, "order_number", "TEST-000000")
            return f"https://pay.mock.test/checkout/{order_number}"
        return f"https://pay.mock.test/checkout/{resolved_reference}"

    async def verify_payment(self, order, business) -> bool:
        return True

    async def handle_webhook(self, payload: dict) -> dict:
        result = {
            "order_id": payload.get("order_id", ""),
            "paid": True,
            "reference": payload.get("reference"),
        }
        if "amount_cents" in payload:
            result["amount_cents"] = payload.get("amount_cents")
        if "currency" in payload:
            result["currency"] = payload.get("currency")
        return result

    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        order_number = getattr(order, "order_number", "TEST-000000")
        return RefundResult(success=True, status="COMPLETED", provider_reference=f"mock_refund_{order_number}")

    async def check_health(self, business) -> ProviderHealthStatus:
        return ProviderHealthStatus.now(healthy=True, provider="MOCK", detail="Test provider — always healthy")
