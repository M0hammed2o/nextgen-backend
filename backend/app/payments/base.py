"""
Abstract PaymentProvider interface.

All payment providers implement this contract.
The order flow never depends on a specific provider — it calls the interface.
"""

from abc import ABC, abstractmethod


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
