"""
Abstract PaymentProvider interface.

All payment providers implement this contract. The order flow (and now the
POS payment step) never depends on a specific provider — it calls the
interface via `backend/app/payments/registry.py::get_provider`.

Adding a new provider: implement this interface in a new module, register it
in `registry.py`'s `_PROVIDERS` dict. Nothing else in the codebase (POS,
order routes, settings) needs to change — this is the whole point of the
framework, not a per-provider special case.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class RefundResult:
    """
    Outcome of a refund attempt.

    status:
      COMPLETED       — the provider confirmed the refund immediately
      PENDING         — the provider accepted the refund request; confirmation
                         will arrive later (webhook, in providers that support one)
      MANUAL_REQUIRED — this provider has no refund API (e.g. Direct EFT) —
                         a human must action the actual money movement themselves;
                         this is a correct, expected outcome, not a failure
      FAILED          — the provider rejected or errored on the refund request
    """
    success: bool
    status: str
    provider_reference: str | None = None
    error: str | None = None


@dataclass
class ProviderHealthStatus:
    """
    Result of a lightweight readiness check for a business's configured
    provider — "is this provider correctly configured and reachable," not a
    full test transaction (which would move real money or require a sandbox
    account most tenants won't have).
    """
    healthy: bool
    provider: str
    checked_at: datetime
    detail: str | None = None

    @classmethod
    def now(cls, healthy: bool, provider: str, detail: str | None = None) -> "ProviderHealthStatus":
        return cls(healthy=healthy, provider=provider, checked_at=datetime.now(timezone.utc), detail=detail)


class PaymentProvider(ABC):
    """Base interface for all payment providers."""

    @abstractmethod
    async def create_payment_link(self, order, business) -> str | None:
        """
        Generate a payment link for the given order.

        Returns:
            URL string on success, None if provider unavailable.
        """

    @abstractmethod
    async def verify_payment(self, order, business) -> bool:
        """
        Query the provider to check whether this order has been paid.

        Returns:
            True if payment confirmed, False otherwise.
        """

    @abstractmethod
    async def handle_webhook(self, payload: dict) -> dict:
        """
        Process an inbound webhook from the provider.

        Returns:
            Dict with keys: order_id (str), paid (bool), reference (str | None).
        """

    @abstractmethod
    async def refund_payment(self, order, business, amount_cents: int) -> RefundResult:
        """
        Refund all or part of a paid order.

        amount_cents may be less than order.total_cents for a partial refund
        (a provider that doesn't support partial refunds should return
        RefundResult(success=False, status="FAILED", error="...") rather than
        silently refunding the full amount).
        """

    @abstractmethod
    async def check_health(self, business) -> ProviderHealthStatus:
        """
        Lightweight readiness check: are this business's credentials present
        and well-formed for this provider? This is a configuration check, not
        a live API ping — verifying real connectivity requires a sandbox
        account most tenants don't have, and is a natural follow-up once a
        specific provider's live credentials are available to test against.
        """
