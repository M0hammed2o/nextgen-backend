"""
Thin Uber Eats API HTTP client — one function per confirmed endpoint, no
business logic. `adapter.py` is the only caller; nothing here decides
whether a call SHOULD be made (capability checks live in `base.py`).

SSRF hardening: every function builds its URL from `settings.
UBER_EATS_API_BASE_URL` plus a validated path segment — never from a
webhook-supplied `resource_href`, which is never read anywhere in this
module.

Response-size cap: Get Order Details "can be very large" per the task brief
— `_MAX_RESPONSE_BYTES` bounds it so a pathological/compromised response
can't exhaust worker memory. gzip is handled transparently by httpx.
"""

from __future__ import annotations

import httpx

from backend.app.core.config import get_settings
from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_TIMEOUT_SECONDS = 15.0


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"}


async def _get(http_client: httpx.AsyncClient, url: str, token: str) -> dict:
    try:
        async with http_client.stream("GET", url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT_SECONDS) as response:
            if response.status_code != 200:
                await response.aread()
                raise DeliveryProviderError(
                    "UBER_EATS", "GET request failed", status_code=response.status_code
                )
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > _MAX_RESPONSE_BYTES:
                    raise DeliveryProviderError(
                        "UBER_EATS", f"response exceeded {_MAX_RESPONSE_BYTES} bytes cap"
                    )
            import json
            return json.loads(bytes(chunks) or b"{}")
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS GET {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc


async def _post(
    http_client: httpx.AsyncClient, url: str, token: str, json_body: dict, *, expect_status: int = 204
) -> None:
    try:
        response = await http_client.post(
            url, headers=_headers(token), json=json_body, timeout=_DEFAULT_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS POST {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc
    if response.status_code != expect_status:
        raise DeliveryProviderError(
            "UBER_EATS", "POST request failed", status_code=response.status_code
        )


def _validated_order_id(order_id: str) -> str:
    """
    Confirms the order id is safe to interpolate into a URL path (alphanumeric
    + hyphen only — Uber order ids are UUID-shaped). Never accept an id
    containing '/', '?', '#', or whitespace — a defensive backstop against a
    malformed/tampered value ever reaching a URL, on top of the caller
    already never sourcing this from a webhook-supplied `resource_href`.
    """
    if not order_id or not all(c.isalnum() or c == "-" for c in order_id):
        raise DeliveryProviderError("UBER_EATS", f"refusing to use unsafe order id: {order_id!r}")
    return order_id


async def get_order_details(http_client: httpx.AsyncClient, token: str, order_id: str) -> dict:
    settings = get_settings()
    order_id = _validated_order_id(order_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v2/eats/order/{order_id}"
    return await _get(http_client, url, token)


async def accept_pos_order(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, body: dict
) -> None:
    settings = get_settings()
    order_id = _validated_order_id(order_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v1/eats/orders/{order_id}/accept_pos_order"
    await _post(http_client, url, token, body, expect_status=204)


async def deny_pos_order(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, body: dict
) -> None:
    settings = get_settings()
    order_id = _validated_order_id(order_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v1/eats/orders/{order_id}/deny_pos_order"
    await _post(http_client, url, token, body, expect_status=204)


async def get_store_status(http_client: httpx.AsyncClient, token: str, store_id: str) -> dict:
    settings = get_settings()
    store_id = _validated_order_id(store_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v1/eats/store/{store_id}/status"
    return await _get(http_client, url, token)


async def set_store_status(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, body: dict
) -> None:
    settings = get_settings()
    store_id = _validated_order_id(store_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v1/eats/store/{store_id}/status"
    try:
        response = await http_client.post(
            url, headers=_headers(token), json=body, timeout=_DEFAULT_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS POST {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc
    if response.status_code not in (200, 204):
        raise DeliveryProviderError(
            "UBER_EATS", "store status update failed", status_code=response.status_code
        )


async def upsert_menu(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, menu_payload: dict
) -> None:
    """
    Menu upsert — signature-only for this change (see
    docs/DELIVERY_PLATFORM_ARCHITECTURE.md §6/§8): no diff-preview/
    confirmation pipeline calls this yet. Implemented as a thin, direct PUT
    so the capability/adapter contract is real rather than a stub, but
    nothing in this codebase invokes it today.
    """
    settings = get_settings()
    store_id = _validated_order_id(store_id)
    url = f"{settings.UBER_EATS_API_BASE_URL}/v2/eats/stores/{store_id}/menus"
    try:
        response = await http_client.put(
            url, headers=_headers(token), json=menu_payload, timeout=30.0
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS PUT {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc
    if response.status_code not in (200, 202, 204):
        raise DeliveryProviderError(
            "UBER_EATS", "menu upsert failed", status_code=response.status_code
        )
