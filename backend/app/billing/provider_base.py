"""
BillingProvider abstraction — NextGen's own SaaS billing of the tenant
(subscription checkout/management/cancellation), kept completely separate
from `backend/app/payments/*` (a tenant's own customers paying the tenant).

Exactly the same shape as the customer-payment PaymentProvider framework:
routes_billing.py never contains provider-specific logic, it only asks the
active provider to "start a subscription" / "let the owner manage it" /
"cancel it." Adding a new billing provider means implementing this
interface and registering it in `registry.py` — nothing else changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BillingCancelResult:
    success: bool
    status: str  # "COMPLETED" | "PENDING" | "MANUAL_REQUIRED" | "FAILED"
    error: str | None = None


class BillingProvider(ABC):
    """Base interface every SaaS-billing provider implements."""

    @abstractmethod
    async def create_checkout_session(self, db, business, plan: str) -> str:
        """Start a new subscription. Returns a URL the owner is redirected to."""

    @abstractmethod
    async def create_portal_session(self, business) -> str | None:
        """
        Return a URL for the owner to self-manage their subscription
        (update card, change plan, cancel), or None if this provider has no
        hosted self-service equivalent — callers must handle None gracefully
        (e.g. show "contact support to change your plan" instead of a link).
        """

    @abstractmethod
    async def cancel_subscription(self, db, business) -> BillingCancelResult:
        """Cancel the business's active subscription with this provider."""

    @abstractmethod
    def supports_self_service_portal(self) -> bool:
        """Whether create_portal_session can ever return a real URL for this provider."""
