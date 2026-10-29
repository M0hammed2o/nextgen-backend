"""
Uber Eats store activation — the one-time, merchant-authorised step that
makes NextGen a store's integration (Uber's "Integration Configuration API").

Until a store is activated, the app's own `client_credentials` token gets
HTTP 401 on that store's endpoints and Uber sends no order webhooks for it.
Flow, per developer.uber.com/docs/eats/guides/integration-activation-flows
(read 2026-10-29):

  1. A merchant user consents on Uber's own login page
     (`authorization_code`, scope `eats.pos_provisioning`). The merchant types
     their password there — NextGen never sees it.
  2. The single-use code (valid 10 minutes) is exchanged for a MERCHANT token.
  3. `GET /v1/eats/stores` with that token lists the merchant's stores.
  4. `POST /v1/eats/stores/{store_id}/pos_data` activates the integration.
     `is_order_manager: true` nominates NextGen to accept/deny/cancel orders.
  5. Uber sends `store.provisioned` to the Primary Webhook URL.

The merchant token is used for these calls only: never cached, stored or
logged. Everything afterwards runs on the app's `client_credentials` token
(`auth.py`). Authorize and token both live on the environment's auth host —
for the sandbox app `https://sandbox-login.uber.com` (probed 2026-10-29: its
`/oauth/v2/authorize` redirects to Uber's login page; production's
`auth.uber.com` rejects the sandbox client as `invalid_client`).
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout
from backend.app.delivery.uber import client
from backend.app.delivery.uber.environments import get_environment_config

PROVISIONING_SCOPE = "eats.pos_provisioning"
_MAX_STORE_PAGES = 20


def build_authorize_url(environment: str, *, redirect_uri: str, state: str) -> str:
    """The link a merchant user opens to consent. `redirect_uri` must be registered on the Uber app."""
    env = get_environment_config(environment)
    if not env.client_id:
        raise DeliveryProviderError("UBER_EATS", f"Uber Eats {env.name} client id is not configured")
    query = urlencode({
        "client_id": env.client_id, "response_type": "code", "scope": PROVISIONING_SCOPE,
        "redirect_uri": redirect_uri, "state": state,
    })
    return f"{env.auth_base_url}/oauth/v2/authorize?{query}"


async def exchange_authorization_code(
    http_client: httpx.AsyncClient, environment: str, *, code: str, redirect_uri: str,
) -> str:
    """Single-use code → merchant access token. Never logs the response body."""
    env = get_environment_config(environment)
    if not env.has_credentials:
        raise DeliveryProviderError("UBER_EATS", f"Uber Eats {env.name} client id/secret are not configured")
    try:
        response = await http_client.post(
            f"{env.auth_base_url}/oauth/v2/token",
            data={
                "client_id": env.client_id,
                "client_secret": env.client_secret,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15.0,
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout("UBER_EATS authorization code exchange timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"authorization code exchange transport error: {exc}") from exc
    if response.status_code != 200:
        raise DeliveryProviderError(
            "UBER_EATS", "authorization code exchange rejected", status_code=response.status_code
        )
    body = response.json()
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise DeliveryProviderError("UBER_EATS", "authorization code exchange returned no access token")
    granted = body.get("scope")
    if isinstance(granted, str) and PROVISIONING_SCOPE not in granted.split():
        raise DeliveryProviderError("UBER_EATS", f"merchant token lacks the {PROVISIONING_SCOPE} scope")
    return token


async def list_merchant_store_ids(
    http_client: httpx.AsyncClient, token: str, *, environment: str,
) -> list[str]:
    """Every store id the merchant token can see (List All Stores, all pages)."""
    api_base_url = get_environment_config(environment).api_base_url
    store_ids: list[str] = []
    start_key = None
    for _ in range(_MAX_STORE_PAGES):
        page = await client.list_stores(http_client, token, api_base_url=api_base_url, start_key=start_key)
        for store in page.get("stores") or []:
            if isinstance(store, dict) and store.get("store_id"):
                store_ids.append(str(store["store_id"]))
        start_key = page.get("next_key")
        if not start_key:
            break
    return store_ids


async def activate_store(
    http_client: httpx.AsyncClient, token: str, *, environment: str, store_id: str, integrator_store_id: str,
) -> None:
    """
    Nominate NextGen as the store's order manager. Only the fields Uber
    documents are sent: `is_order_manager` and `integrator_store_id` (NextGen's
    own id for the store — the business id). `pos_integration_enabled` is
    documented as deprecated and ignored, so it is never sent.
    """
    await client.activate_integration(
        http_client, token, store_id,
        body={"is_order_manager": True, "integrator_store_id": integrator_store_id},
        api_base_url=get_environment_config(environment).api_base_url,
    )
