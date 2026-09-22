"""
Abstract `DeliveryProviderAdapter` interface — modelled on the existing
`PaymentProvider(ABC)` pattern (`backend/app/payments/base.py`). Adding a
new provider means implementing this interface in a new module and
registering it in `registry.py`'s provider table; nothing else in the
codebase (the webhook route, the worker jobs) needs to change.

THE CAPABILITY GUARD IS ENFORCED BY THE ABC, NOT BY CONVENTION
------------------------------------------------------------------
Every PUBLIC method on this class is concrete, not abstract: it calls
`self._require(capability)` first and only then delegates to a
subclass-implemented private `_xxx` method. A subclass can override the
private method, but it can never skip the capability check by overriding
the public one differently, because nothing else in this codebase calls the
private methods directly. This is what "enforced by the ABC itself so a
subclass can't forget" (design doc §1) actually means in code — a docstring
reminder would not have been enough (see `mrd/adapter.py`, which relies on
exactly this to make every network call impossible before it starts).

`DeliveryOrderContext` (not the live `DeliveryConnection` ORM row) is what
every network method takes — plain primitives, not a SQLAlchemy model, so
an adapter's methods are unit-testable with zero database/ORM setup, same
discipline as `CanonicalDeliveryOrder` in `canonical.py`. Decrypting the
stored credential (if any) is the caller's job (`order_service.py`), not
the adapter's — an adapter never touches `shared.models` at all.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from backend.app.delivery.canonical import (
    CanonicalDeliveryOrder,
    DeliveryOrderStatus,
    WebhookEnvelope,
)
from backend.app.delivery.errors import DeliveryCapabilityUnsupported


@dataclass(frozen=True)
class DeliveryCapabilities:
    """
    Static per-adapter-CLASS capability flags — never DB-stored, precisely
    to avoid the code/DB drift class this repo has already hit once
    (KNOWN_BUGS.md BUG-0003). See design doc §1's capability table.
    """
    webhooks: bool = False
    accept_deny: bool = False
    menu_sync: bool = False
    store_pause_resume: bool = False
    # Not confirmed in Uber's official docs — treated as unsupported for
    # every adapter today. v1 item-level 86ing is therefore NextGen-internal
    # (see availability handling in order_service.py), not a provider call.
    item_availability: bool = False
    store_discovery: bool = False
    # Phase 2, not built in v1 — needs the separate eats.report OAuth scope.
    settlement_report: bool = False


@dataclass(frozen=True)
class DeliveryOrderContext:
    """
    Everything a network-calling adapter method needs about the connection
    it's acting on, without needing the live ORM row or a DB session.
    """
    business_id: uuid.UUID
    connection_id: uuid.UUID
    external_store_id: str
    environment: str = "production"
    # Decrypted operational credential material, when the adapter needs one
    # beyond its own cached platform token (Uber's client_credentials token
    # is cached by the adapter/auth layer itself, never passed in here).
    decrypted_provisioning_token: str | None = None


class DeliveryProviderAdapter(ABC):
    """Base interface for all delivery-marketplace provider adapters."""

    #: Set by every concrete subclass, e.g. "UBER_EATS" / "MR_D" — must
    #: match `shared.enums.ExternalPlatform` and the `provider` column
    #: values used throughout `shared/models/delivery.py`.
    provider: str
    capabilities: DeliveryCapabilities

    def _require(self, capability: str) -> None:
        if not getattr(self.capabilities, capability, False):
            raise DeliveryCapabilityUnsupported(self.provider, capability)

    # ── Webhook plane — PURE, no network, no database ────────────────────

    def verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """
        Verify the raw request body against the provider's signature header.
        Callers MUST read `await request.body()` before any parsing and pass
        those exact raw bytes here — the signature is over the raw body, not
        a re-serialised parse of it.
        """
        self._require("webhooks")
        return self._verify_webhook_signature(raw_body, headers)

    @abstractmethod
    def _verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_webhook_event(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookEnvelope | None:
        """
        Pure parse of the webhook envelope — event id/type, the reported
        store id, the reported order id. Returns None for a body this
        adapter cannot recognise at all (caller treats that as a parse
        failure, still acked 200, still logged with an error_category).

        MUST NOT follow any self-reported "fetch more here" URL in the
        payload (e.g. Uber's `resource_href`) — that is an SSRF path this
        method exists specifically to avoid opening.
        """
        self._require("webhooks")
        return self._parse_webhook_event(raw_body, headers)

    @abstractmethod
    def _parse_webhook_event(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookEnvelope | None: ...

    # ── Order plane — network ─────────────────────────────────────────────

    async def fetch_order(
        self, *, external_order_id: str, context: DeliveryOrderContext
    ) -> CanonicalDeliveryOrder:
        """
        Get Order Details. The fetch URL is always reconstructed from
        server-side settings plus `external_order_id` — never from a
        webhook-supplied URL (see `parse_webhook_event`).
        """
        self._require("webhooks")
        return await self._fetch_order(external_order_id=external_order_id, context=context)

    @abstractmethod
    async def _fetch_order(
        self, *, external_order_id: str, context: DeliveryOrderContext
    ) -> CanonicalDeliveryOrder: ...

    async def accept_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        our_order_id: str,
    ) -> None:
        self._require("accept_deny")
        return await self._accept_order(
            external_order_id=external_order_id, context=context, our_order_id=our_order_id,
        )

    @abstractmethod
    async def _accept_order(
        self, *, external_order_id: str, context: DeliveryOrderContext, our_order_id: str
    ) -> None: ...

    async def deny_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None = None,
    ) -> None:
        self._require("accept_deny")
        return await self._deny_order(
            external_order_id=external_order_id,
            context=context,
            reason_code=reason_code,
            explanation=explanation,
        )

    @abstractmethod
    async def _deny_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None,
    ) -> None: ...

    # ── Store plane — network ─────────────────────────────────────────────

    async def get_store_status(self, *, context: DeliveryOrderContext) -> dict:
        self._require("store_pause_resume")
        return await self._get_store_status(context=context)

    @abstractmethod
    async def _get_store_status(self, *, context: DeliveryOrderContext) -> dict: ...

    async def set_store_status(
        self,
        *,
        context: DeliveryOrderContext,
        status: str,
        paused_until=None,
        reason: str | None = None,
    ) -> None:
        self._require("store_pause_resume")
        return await self._set_store_status(
            context=context, status=status, paused_until=paused_until, reason=reason,
        )

    @abstractmethod
    async def _set_store_status(
        self, *, context: DeliveryOrderContext, status: str, paused_until, reason: str | None
    ) -> None: ...

    # ── Menu sync — SIGNATURE ONLY for this change ────────────────────────
    #
    # Deliberately out of scope beyond the method existing behind its
    # capability flag (design doc §6/§8) — no diff-preview/confirmed-push
    # pipeline is built in this change, and nothing in this codebase calls
    # this method yet.

    async def upsert_menu(self, *, context: DeliveryOrderContext, menu_payload: dict) -> None:
        self._require("menu_sync")
        return await self._upsert_menu(context=context, menu_payload=menu_payload)

    @abstractmethod
    async def _upsert_menu(self, *, context: DeliveryOrderContext, menu_payload: dict) -> None: ...

    # ── Pure mapping — no capability gate; every adapter must always be ──
    #    able to say what a provider status means, even a disabled stub.

    @abstractmethod
    def map_order_status(self, canonical: CanonicalDeliveryOrder) -> DeliveryOrderStatus:
        """
        Provider status string -> canonical `DeliveryOrderStatus`. Pure,
        same discipline as `external_orders/adapters.py`. An unrecognised
        provider status maps to `DeliveryOrderStatus.UNKNOWN` and the caller
        raises an alert — never a guess.
        """
