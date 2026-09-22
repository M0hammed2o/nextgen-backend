"""
Uber Eats OAuth — two separate token families that must NEVER be mixed on
one token (design doc §1):

1. OPERATIONAL token — `client_credentials` grant, platform-wide (one
   NextGen app, not per-merchant), scopes `eats.store` /
   `eats.store.status.write` / `eats.order` / `eats.store.orders.read` /
   `eats.report`. Lives 30 days (2592000s). Minting is rate-limited to
   100/hour and the 101st mint INVALIDATES THE OLDEST — so this module
   caches the token in Redis behind a mint lock and never mints per
   request, and NEVER stores it in Postgres (unlike a payment-provider
   credential, this is not tenant data at all — it belongs to the platform
   app registration).
2. PROVISIONING token — separate `authorization_code` grant, scope
   `eats.pos_provisioning` ONLY (cannot be combined with the scopes above
   on one token), one per merchant during store activation. That token is
   handled entirely in `provisioning`-flow code (not built in this change —
   menu sync / store discovery pipeline is out of scope, see
   `docs/DELIVERY_PLATFORM_ARCHITECTURE.md` §6/§8) and, once a live
   connection exists, is Fernet-encrypted on `DeliveryConnection.
   provisioning_token_encrypted` and wiped after activation — never routed
   through this module.

Endpoint (confirmed): `POST https://auth.uber.com/oauth/v2/token`
(`https://sandbox-auth.uber.com/...` in sandbox — see
`settings.UBER_EATS_AUTH_BASE_URL`).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from redis.exceptions import RedisError

from backend.app.core.config import get_settings
from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout

logger = logging.getLogger("nextgen.delivery.uber.auth")

_TOKEN_CACHE_KEY = "uber_eats:oauth:operational_token"
_MINT_LOCK_KEY = "uber_eats:oauth:mint_lock"
_MINT_LOCK_TTL_SECONDS = 20
_LOCK_WAIT_ITERATIONS = 10
_LOCK_WAIT_INTERVAL_SECONDS = 0.5
# Refresh a bit before the real 2592000s (30-day) expiry so an in-flight
# request never gets handed a token that expires mid-call.
_TOKEN_REFRESH_MARGIN_SECONDS = 3600

_OPERATIONAL_SCOPES = (
    "eats.store eats.store.status.write eats.order eats.store.orders.read"
)


class OperationalTokenUnavailable(DeliveryProviderError):
    """Could not obtain a usable Uber Eats operational token."""

    def __init__(self, message: str):
        super().__init__("UBER_EATS", message)


async def _mint_operational_token(http_client: httpx.AsyncClient) -> tuple[str, int]:
    """
    A single, direct call to the token endpoint. Callers MUST hold the
    Redis mint lock before calling this — see `get_operational_token()`.
    Never call this per-request; that is exactly the 100/hour-mint,
    invalidate-the-oldest trap this module exists to avoid.
    """
    settings = get_settings()
    if not settings.UBER_EATS_CLIENT_ID or not settings.UBER_EATS_CLIENT_SECRET:
        raise OperationalTokenUnavailable(
            "UBER_EATS_CLIENT_ID/UBER_EATS_CLIENT_SECRET are not configured"
        )
    try:
        response = await http_client.post(
            f"{settings.UBER_EATS_AUTH_BASE_URL}/oauth/v2/token",
            data={
                "client_id": settings.UBER_EATS_CLIENT_ID,
                "client_secret": settings.UBER_EATS_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": _OPERATIONAL_SCOPES,
            },
            timeout=15.0,
        )
    except httpx.TimeoutException as exc:
        raise DeliveryProviderTimeout("UBER_EATS oauth token mint timed out") from exc
    except httpx.HTTPError as exc:
        raise DeliveryProviderError("UBER_EATS", f"oauth token mint transport error: {exc}") from exc

    if response.status_code != 200:
        # Never log the response body — it can carry error detail that
        # itself echoes back request parameters.
        raise DeliveryProviderError(
            "UBER_EATS", "oauth token mint rejected", status_code=response.status_code
        )
    body = response.json()
    token = body.get("access_token")
    expires_in = body.get("expires_in")
    if not token or not isinstance(expires_in, int):
        raise DeliveryProviderError("UBER_EATS", "oauth token mint returned a malformed body")
    return token, expires_in


async def get_operational_token(redis, http_client: httpx.AsyncClient) -> str:
    """
    Return a cached operational token, minting a new one (behind a
    distributed lock) only when the cache is empty.

    Redis unavailability fails CLOSED — raises rather than falling back to
    minting per-request, since the whole point of the cache is staying well
    under the 100/hour mint limit.
    """
    try:
        cached = await redis.get(_TOKEN_CACHE_KEY)
    except RedisError as exc:
        raise OperationalTokenUnavailable(f"Redis unavailable: {exc}") from exc
    if cached:
        return cached

    try:
        got_lock = await redis.set(_MINT_LOCK_KEY, "1", nx=True, ex=_MINT_LOCK_TTL_SECONDS)
    except RedisError as exc:
        raise OperationalTokenUnavailable(f"Redis unavailable: {exc}") from exc

    if not got_lock:
        # Another process is minting right now — poll briefly rather than
        # mint concurrently, which is exactly how two processes would burn
        # through the 100/hour budget for no reason.
        for _ in range(_LOCK_WAIT_ITERATIONS):
            await asyncio.sleep(_LOCK_WAIT_INTERVAL_SECONDS)
            try:
                cached = await redis.get(_TOKEN_CACHE_KEY)
            except RedisError as exc:
                raise OperationalTokenUnavailable(f"Redis unavailable: {exc}") from exc
            if cached:
                return cached
        raise OperationalTokenUnavailable(
            "Timed out waiting for another process to finish minting an operational token"
        )

    try:
        token, expires_in = await _mint_operational_token(http_client)
        ttl = max(expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 60)
        try:
            await redis.set(_TOKEN_CACHE_KEY, token, ex=ttl)
        except RedisError as exc:
            # The mint succeeded but we couldn't cache it — still usable for
            # THIS call, but log loudly since the next call will mint again.
            logger.warning("UBER_EATS_TOKEN_CACHE_WRITE_FAILED: %s", exc)
        return token
    finally:
        try:
            await redis.delete(_MINT_LOCK_KEY)
        except RedisError:
            logger.warning("UBER_EATS_MINT_LOCK_RELEASE_FAILED — will expire via TTL")
