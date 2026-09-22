"""
Mr D adapter — DISABLED STUB.

DO NOT ADD A REAL ENDPOINT, HEADER, OR AUTH SCHEME HERE
------------------------------------------------------------
No Mr D API is publicly documented (see `docs/MRD_API_ACCESS_2026-10.md`).
Every capability flag on this adapter is False, every network method raises
`DeliveryProviderNotImplemented` unconditionally, and
`verify_webhook_signature` returns False unconditionally — not because a
real signature check failed, but because there is no real scheme to check
against, and a stub that claimed to validate a signature would be worse
than one that visibly refuses.

The existing Mr D CSV import (`backend/app/external_orders/`) is untouched
by this module and remains the only live Mr D integration. `MR_D` is
additionally absent from `registry.py::_WEBHOOK_ENABLED_PROVIDERS`, so no
webhook request for this provider ever reaches this class's methods at all
— the checks below are defence in depth, not the only thing preventing a
live Mr D network path from existing.
"""

from __future__ import annotations

from typing import Mapping

from backend.app.delivery.base import (
    DeliveryCapabilities,
    DeliveryOrderContext,
    DeliveryProviderAdapter,
)
from backend.app.delivery.canonical import (
    CanonicalDeliveryOrder,
    DeliveryOrderStatus,
    WebhookEnvelope,
)
from backend.app.delivery.errors import DeliveryProviderNotImplemented


class MrDAdapter(DeliveryProviderAdapter):
    provider = "MR_D"
    capabilities = DeliveryCapabilities()  # every flag defaults False

    def _verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        # Not "signature invalid" — there is no scheme to validate against.
        # Returning False unconditionally, never True, is the only honest
        # answer a stub with no real crypto can give.
        return False

    def _parse_webhook_event(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookEnvelope | None:
        raise DeliveryProviderNotImplemented(self.provider, "parse_webhook_event")

    async def _fetch_order(
        self, *, external_order_id: str, context: DeliveryOrderContext
    ) -> CanonicalDeliveryOrder:
        raise DeliveryProviderNotImplemented(self.provider, "fetch_order")

    async def _accept_order(
        self, *, external_order_id: str, context: DeliveryOrderContext, our_order_id: str
    ) -> None:
        raise DeliveryProviderNotImplemented(self.provider, "accept_order")

    async def _deny_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None,
    ) -> None:
        raise DeliveryProviderNotImplemented(self.provider, "deny_order")

    async def _get_store_status(self, *, context: DeliveryOrderContext) -> dict:
        raise DeliveryProviderNotImplemented(self.provider, "get_store_status")

    async def _set_store_status(
        self, *, context: DeliveryOrderContext, status: str, paused_until, reason: str | None
    ) -> None:
        raise DeliveryProviderNotImplemented(self.provider, "set_store_status")

    async def _upsert_menu(self, *, context: DeliveryOrderContext, menu_payload: dict) -> None:
        raise DeliveryProviderNotImplemented(self.provider, "upsert_menu")

    def map_order_status(self, canonical: CanonicalDeliveryOrder) -> DeliveryOrderStatus:
        return DeliveryOrderStatus.UNKNOWN
