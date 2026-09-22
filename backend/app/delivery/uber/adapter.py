"""
UberEatsAdapter — the only concrete, live `DeliveryProviderAdapter` in this
release. See `base.py` for why every public method here is inherited
concrete (capability-guarded) and only the `_xxx` methods below are this
class's own code.
"""

from __future__ import annotations

import logging
from typing import Mapping

import httpx

from backend.app.core.config import get_settings
from backend.app.db.session import get_redis
from backend.app.delivery.base import (
    DeliveryCapabilities,
    DeliveryOrderContext,
    DeliveryProviderAdapter,
)
from backend.app.delivery.canonical import CanonicalDeliveryOrder, DeliveryOrderStatus, WebhookEnvelope
from backend.app.delivery.uber import client as uber_client
from backend.app.delivery.uber import mapping as uber_mapping
from backend.app.delivery.uber.auth import get_operational_token

logger = logging.getLogger("nextgen.delivery.uber")

_DENY_REASON_CODES = frozenset({
    "STORE_CLOSED", "POS_NOT_READY", "POS_OFFLINE", "ITEM_AVAILABILITY",
    "MISSING_ITEM", "MISSING_INFO", "PRICING", "CAPACITY", "ADDRESS",
    "SPECIAL_INSTRUCTIONS", "OTHER",
})

_shared_http_client: httpx.AsyncClient | None = None


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


class UberEatsAdapter(DeliveryProviderAdapter):
    provider = "UBER_EATS"
    capabilities = DeliveryCapabilities(
        webhooks=True,
        accept_deny=True,
        menu_sync=True,
        store_pause_resume=True,
        item_availability=False,  # not confirmed in official docs
        store_discovery=True,
        settlement_report=False,  # Phase 2 — needs eats.report scope
    )

    # ── Webhook plane (pure) ──────────────────────────────────────────────

    def _verify_webhook_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        settings = get_settings()
        header_value = headers.get("x-uber-signature") or headers.get("X-Uber-Signature")
        return uber_mapping.verify_signature(raw_body, header_value, settings.UBER_EATS_CLIENT_SECRET)

    def _parse_webhook_event(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> WebhookEnvelope | None:
        import json

        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError):
            return None
        return uber_mapping.parse_webhook_envelope(payload)

    # ── Order plane (network) ─────────────────────────────────────────────

    async def _fetch_order(
        self, *, external_order_id: str, context: DeliveryOrderContext
    ) -> CanonicalDeliveryOrder:
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        raw = await uber_client.get_order_details(http_client, token, external_order_id)
        return uber_mapping.parse_order_details(raw, external_store_id=context.external_store_id)

    async def _accept_order(
        self, *, external_order_id: str, context: DeliveryOrderContext, our_order_id: str
    ) -> None:
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        body = {
            "reason": "Order accepted",
            # Round-trips our own order id back through Uber, per the
            # confirmed field list — lets a later webhook/report correlate
            # back to the ORDER we created, not just the delivery_orders row.
            "external_reference_id": str(our_order_id),
        }
        await uber_client.accept_pos_order(http_client, token, external_order_id, body=body)

    async def _deny_order(
        self,
        *,
        external_order_id: str,
        context: DeliveryOrderContext,
        reason_code: str,
        explanation: str | None,
    ) -> None:
        if reason_code not in _DENY_REASON_CODES:
            logger.warning(
                "UBER_EATS_DENY_UNKNOWN_REASON_CODE: %s not in the documented set — "
                "sending OTHER instead.", reason_code,
            )
            reason_code = "OTHER"
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        body = {
            "reason": {
                "explanation": explanation or reason_code,
                "code": reason_code,
                "out_of_stock_items": [],
                "invalid_items": [],
            },
        }
        await uber_client.deny_pos_order(http_client, token, external_order_id, body=body)

    # ── Store plane (network) ──────────────────────────────────────────────

    async def _get_store_status(self, *, context: DeliveryOrderContext) -> dict:
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        return await uber_client.get_store_status(http_client, token, context.external_store_id)

    async def _set_store_status(
        self, *, context: DeliveryOrderContext, status: str, paused_until, reason: str | None
    ) -> None:
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        body = {"status": status}
        if paused_until is not None:
            body["paused_until"] = paused_until
        if reason is not None:
            body["reason"] = reason
        await uber_client.set_store_status(http_client, token, context.external_store_id, body=body)

    # ── Menu sync — signature only, see base.py/client.py ──────────────────

    async def _upsert_menu(self, *, context: DeliveryOrderContext, menu_payload: dict) -> None:
        redis = await get_redis()
        http_client = _get_http_client()
        token = await get_operational_token(redis, http_client)
        await uber_client.upsert_menu(
            http_client, token, context.external_store_id, menu_payload=menu_payload
        )

    # ── Pure mapping ─────────────────────────────────────────────────────

    def map_order_status(self, canonical: CanonicalDeliveryOrder) -> DeliveryOrderStatus:
        return uber_mapping.map_current_state(canonical.provider_status_raw)
