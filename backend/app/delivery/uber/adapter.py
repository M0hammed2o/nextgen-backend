"""
UberEatsAdapter — the only concrete, live `DeliveryProviderAdapter` in this
release. See `base.py` for why every public method here is inherited
concrete (capability-guarded) and only the `_xxx` methods below are this
class's own code.

Every network call resolves credentials and hosts from the CONNECTION's
environment (`context.environment`, see `environments.py`), so production
and sandbox never share a token, a host, or an order.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Mapping, TypeVar

import httpx

from backend.app.db.session import get_redis
from backend.app.delivery.base import (
    DeliveryCapabilities,
    DeliveryOrderContext,
    DeliveryProviderAdapter,
)
from backend.app.delivery.canonical import (
    CanonicalCatalogueEntry,
    CanonicalDeliveryOrder,
    DeliveryOrderStatus,
    WebhookEnvelope,
)
from backend.app.delivery.errors import DeliveryProviderError
from backend.app.delivery.uber import client as uber_client
from backend.app.delivery.uber import mapping as uber_mapping
from backend.app.delivery.uber.auth import get_operational_token, invalidate_operational_token
from backend.app.delivery.uber.environments import (
    get_environment_config,
    match_webhook_environment,
)

logger = logging.getLogger("nextgen.delivery.uber")

#: deny_pos_order reason codes, exactly as Uber's reference lists them.
_DENY_REASON_CODES = frozenset({
    "STORE_CLOSED", "POS_NOT_READY", "POS_OFFLINE", "ITEM_AVAILABILITY",
    "MISSING_ITEM", "MISSING_INFO", "PRICING", "CAPACITY", "ADDRESS",
    "SPECIAL_INSTRUCTIONS", "OTHER",
})

#: Restaurant-cancel reasons (`POST /v1/eats/orders/{id}/cancel`), exactly as
#: Uber's reference lists them. `details` is required when the reason is OTHER.
CANCEL_REASON_CODES = frozenset({
    "OUT_OF_ITEMS", "KITCHEN_CLOSED", "CUSTOMER_CALLED_TO_CANCEL",
    "RESTAURANT_TOO_BUSY", "CANNOT_COMPLETE_CUSTOMER_NOTE", "OTHER",
})

_shared_http_client: httpx.AsyncClient | None = None

T = TypeVar("T")


def _get_http_client() -> httpx.AsyncClient:
    """
    One shared connection-pooled client for the process. Not created at
    import time (keeps import side-effect-free for tests) — lazily built on
    first real network call.
    """
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient()
    return _shared_http_client


#: At most one forced re-mint per environment per this many seconds. The
#: token is platform-wide and Uber caps client_credentials mints at 100/hour
#: (the 101st invalidates the oldest), so a 401 that is NOT about the token
#: (e.g. a store-specific refusal) must not let repeated clicks or retries
#: churn the token every tenant shares.
_REMINT_COOLDOWN_SECONDS = 60


async def _call(context: DeliveryOrderContext, request: Callable[[httpx.AsyncClient, str, str], Awaitable[T]]) -> T:
    """
    Run one API call with this connection's environment token and host.

    A 401 usually means the cached token was revoked/rotated on Uber's side:
    drop it and retry once with a freshly minted token, so a bad token does
    not keep failing until its 30-day cache expiry. Re-mints are rate-limited
    per environment (`_REMINT_COOLDOWN_SECONDS`); inside the cooldown the 401
    is simply raised.
    """
    env = get_environment_config(context.environment)
    redis = await get_redis()
    http_client = _get_http_client()
    token = await get_operational_token(redis, http_client, env.name)
    try:
        return await request(http_client, token, env.api_base_url)
    except DeliveryProviderError as exc:
        if exc.status_code != 401:
            raise
        try:
            may_remint = await redis.set(
                f"uber_eats:{env.name}:oauth:remint_cooldown", "1",
                nx=True, ex=_REMINT_COOLDOWN_SECONDS,
            )
        except Exception:
            may_remint = False
        if not may_remint:
            raise
        logger.warning("UBER_EATS_TOKEN_REJECTED: environment=%s — re-minting once.", env.name)
        await invalidate_operational_token(redis, env.name)
        token = await get_operational_token(redis, http_client, env.name)
        return await request(http_client, token, env.api_base_url)


class UberEatsAdapter(DeliveryProviderAdapter):
    provider = "UBER_EATS"
    capabilities = DeliveryCapabilities(
        webhooks=True,
        accept_deny=True,
        menu_sync=True,
        store_pause_resume=True,
        # A per-item update endpoint exists, but Uber warns it can 404 until a
        # full menu has been uploaded via PUT — which NextGen deliberately does
        # not do (irreversible side effects). Item 86ing therefore stays
        # NextGen-internal: an order for an unavailable item is denied with
        # ITEM_AVAILABILITY (order_service.py).
        item_availability=False,
        store_discovery=True,
        settlement_report=False,  # Phase 2 — needs eats.report scope
        ready_signal=True,
        restaurant_cancel=True,
        catalogue_read=True,
    )

    # ── Webhook plane (pure) ──────────────────────────────────────────────

    def _verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return self._verified_webhook_environment(raw_body, headers) is not None

    def _verified_webhook_environment(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> str | None:
        header_value = headers.get("x-uber-signature") or headers.get("X-Uber-Signature")
        return match_webhook_environment(raw_body, header_value)

    def _parse_webhook_event(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookEnvelope | None:
        import json

        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError, RecursionError):
            return None
        return uber_mapping.parse_webhook_envelope(payload)

    # ── Order plane (network) ─────────────────────────────────────────────

    async def _fetch_order(
        self, *, external_order_id: str, context: DeliveryOrderContext
    ) -> CanonicalDeliveryOrder:
        raw = await _call(context, lambda c, t, base: uber_client.get_order_details(
            c, t, external_order_id, api_base_url=base,
        ))
        return uber_mapping.parse_order_details(raw, external_store_id=context.external_store_id)

    async def _accept_order(
        self, *, external_order_id: str, context: DeliveryOrderContext, our_order_id: str
    ) -> None:
        body = {
            "reason": "Order accepted",
            # Round-trips our own order id back through Uber, per the
            # documented field list — lets a later webhook/report correlate
            # back to the ORDER we created, not just the delivery_orders row.
            "external_reference_id": str(our_order_id),
        }
        await _call(context, lambda c, t, base: uber_client.accept_pos_order(
            c, t, external_order_id, body=body, api_base_url=base,
        ))

    async def _deny_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None,
        out_of_stock_items: list[str] | None = None,
    ) -> None:
        if reason_code not in _DENY_REASON_CODES:
            logger.warning(
                "UBER_EATS_DENY_UNKNOWN_REASON_CODE: %s not in the documented set — "
                "sending OTHER instead.", reason_code,
            )
            reason_code = "OTHER"
        body = {
            "reason": {
                "explanation": explanation or reason_code,
                "code": reason_code,
                "out_of_stock_items": list(out_of_stock_items or []),
                "invalid_items": [],
            },
        }
        await _call(context, lambda c, t, base: uber_client.deny_pos_order(
            c, t, external_order_id, body=body, api_base_url=base,
        ))

    async def _cancel_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None,
    ) -> None:
        if reason_code not in CANCEL_REASON_CODES:
            logger.warning(
                "UBER_EATS_CANCEL_UNKNOWN_REASON_CODE: %s not in the documented set — "
                "sending OTHER instead.", reason_code,
            )
            reason_code = "OTHER"
        body: dict = {"reason": reason_code}
        if explanation or reason_code == "OTHER":
            # Required by Uber when the reason is OTHER.
            body["details"] = (explanation or "Cancelled by the restaurant")[:500]
        await _call(context, lambda c, t, base: uber_client.cancel_order(
            c, t, external_order_id, body=body, api_base_url=base,
        ))

    async def _mark_order_ready(
        self, *, external_order_id: str, context: DeliveryOrderContext,
    ) -> None:
        await _call(context, lambda c, t, base: uber_client.mark_order_ready(
            c, t, external_order_id, api_base_url=base,
        ))

    # ── Store plane (network) ──────────────────────────────────────────────

    async def _get_store_status(self, *, context: DeliveryOrderContext) -> dict:
        return await _call(context, lambda c, t, base: uber_client.get_store_status(
            c, t, context.external_store_id, api_base_url=base,
        ))

    async def _set_store_status(
        self, *, context: DeliveryOrderContext, status: str, paused_until, reason: str | None
    ) -> None:
        body = {"status": status}
        if paused_until is not None:
            body["paused_until"] = paused_until
        if reason is not None:
            body["reason"] = reason
        await _call(context, lambda c, t, base: uber_client.set_store_status(
            c, t, context.external_store_id, body=body, api_base_url=base,
        ))

    # ── Menu (read for mapping import; write is signature-only) ───────────

    async def _fetch_menu_catalogue(
        self, *, context: DeliveryOrderContext,
    ) -> list[CanonicalCatalogueEntry]:
        raw = await _call(context, lambda c, t, base: uber_client.get_menu(
            c, t, context.external_store_id, api_base_url=base,
        ))
        return uber_mapping.parse_menu_catalogue(raw)

    async def _upsert_menu(self, *, context: DeliveryOrderContext, menu_payload: dict) -> None:
        await _call(context, lambda c, t, base: uber_client.upsert_menu(
            c, t, context.external_store_id, menu_payload=menu_payload, api_base_url=base,
        ))

    # ── Pure mapping ─────────────────────────────────────────────────────

    def map_order_status(self, canonical: CanonicalDeliveryOrder) -> DeliveryOrderStatus:
        return uber_mapping.map_current_state(canonical.provider_status_raw)
