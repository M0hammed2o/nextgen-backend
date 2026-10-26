"""
Thin Uber Eats API HTTP client — one function per documented endpoint, no
business logic. `adapter.py` is the only caller; nothing here decides
whether a call SHOULD be made (capability checks live in `base.py`).

Every function takes the environment's `api_base_url` explicitly
(`environments.py`) — a sandbox order is never sent to production hosts or
the reverse.

SSRF hardening: every URL is built from that configured base URL plus a
validated path segment — never from a webhook-supplied `resource_href`,
which is never read anywhere in this module.

Response-size cap: Get Order Details "may be very large" per Uber's own
reference — `_MAX_RESPONSE_BYTES` bounds it so a pathological/compromised
response can't exhaust worker memory. gzip is handled transparently by httpx.

API VERSIONS USED (developer.uber.com/docs/eats, read 2026-10-26)
-------------------------------------------------------------------
Get order / accept / deny / cancel / store status / menus are Uber's
documented, non-deprecated v1/v2 Eats endpoints (now labelled "Previous
Version" beside a newer Order Fulfillment API; nothing is deprecated, and
Uber's policy is 3 months' support after any deprecation notice). Marking an
order ready for pickup exists ONLY in the newer API
(`POST /v1/delivery/order/{id}/ready`), so that one call uses it. Which
version Uber certifies a new integration against is an open question for
Uber's integration team — see docs/UBER_EATS_INTEGRATION_2026-10.md.
"""

from __future__ import annotations

import json
import re

import httpx

from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB
_SAFE_ID = re.compile(r"[A-Za-z0-9-]{1,128}")
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
            return json.loads(bytes(chunks) or b"{}")
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS GET {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc


async def _post(
    http_client: httpx.AsyncClient, url: str, token: str, json_body: dict,
    *, expect_status: tuple[int, ...] = (204,),
) -> None:
    try:
        response = await http_client.post(
            url, headers=_headers(token), json=json_body, timeout=_DEFAULT_TIMEOUT_SECONDS
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout(f"UBER_EATS POST {url} timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"transport error: {exc}") from exc
    if response.status_code not in expect_status:
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
    # ASCII only: str.isalnum() also accepts characters such as "²" or "Ａ".
    if not order_id or not _SAFE_ID.fullmatch(order_id):
        raise DeliveryProviderError("UBER_EATS", f"refusing to use unsafe order id: {order_id!r}")
    return order_id


async def get_order_details(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, api_base_url: str,
) -> dict:
    order_id = _validated_order_id(order_id)
    return await _get(http_client, f"{api_base_url}/v2/eats/order/{order_id}", token)


async def accept_pos_order(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, body: dict, api_base_url: str,
) -> None:
    order_id = _validated_order_id(order_id)
    url = f"{api_base_url}/v1/eats/orders/{order_id}/accept_pos_order"
    await _post(http_client, url, token, body, expect_status=(204,))


async def deny_pos_order(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, body: dict, api_base_url: str,
) -> None:
    order_id = _validated_order_id(order_id)
    url = f"{api_base_url}/v1/eats/orders/{order_id}/deny_pos_order"
    await _post(http_client, url, token, body, expect_status=(204,))


async def cancel_order(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, body: dict, api_base_url: str,
) -> None:
    """
    Restaurant-initiated cancel of an already-accepted order. Documented
    response is `200 {}`; 204 is also accepted. A 404 means "already in a
    terminal state, or never existed" — surfaced to the caller as an error
    like any other non-2xx.
    """
    order_id = _validated_order_id(order_id)
    url = f"{api_base_url}/v1/eats/orders/{order_id}/cancel"
    await _post(http_client, url, token, body, expect_status=(200, 204))


async def mark_order_ready(
    http_client: httpx.AsyncClient, token: str, order_id: str, *, api_base_url: str,
) -> None:
    """Order Fulfillment API "Order Ready" — body `{}`, documented `200`."""
    order_id = _validated_order_id(order_id)
    url = f"{api_base_url}/v1/delivery/order/{order_id}/ready"
    await _post(http_client, url, token, {}, expect_status=(200, 204))


async def get_store_status(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, api_base_url: str,
) -> dict:
    store_id = _validated_order_id(store_id)
    return await _get(http_client, f"{api_base_url}/v1/eats/store/{store_id}/status", token)


async def set_store_status(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, body: dict, api_base_url: str,
) -> None:
    store_id = _validated_order_id(store_id)
    url = f"{api_base_url}/v1/eats/store/{store_id}/status"
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


async def get_menu(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, api_base_url: str,
) -> dict:
    """
    v2 Get Menu (scope `eats.store`) — READ ONLY. Feeds the mapping
    screen's catalogue import; nothing in this codebase writes a menu back.
    """
    store_id = _validated_order_id(store_id)
    return await _get(http_client, f"{api_base_url}/v2/eats/stores/{store_id}/menus", token)


async def upsert_menu(
    http_client: httpx.AsyncClient, token: str, store_id: str, *, menu_payload: dict, api_base_url: str,
) -> None:
    """
    Menu upsert — signature-only (see docs/DELIVERY_PLATFORM_ARCHITECTURE.md
    §6/§8): no diff-preview/confirmation pipeline calls this, because a
    full-menu PUT has irreversible side effects on Uber's side (an alcohol
    flag can never be unset via the API; a non-default menu_type forks the
    menu permanently). Nothing in this codebase invokes it today.
    """
    store_id = _validated_order_id(store_id)
    url = f"{api_base_url}/v2/eats/stores/{store_id}/menus"
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
