"""
Anti-CSRF / anti-replay state token for the WhatsApp Embedded Signup connect
flow (gate hardening pass, 2026-09-22).

Why this exists (the gap): POST /exchange was already JWT-authenticated (the
caller must be a real, logged-in OWNER of `business_id`), but JWT auth alone
does not prove that THIS specific /exchange call is the one deliberately
completing THIS specific signup attempt from THIS specific browser session.
A forged/replayed cross-site request, a stale browser tab left open from a
previous attempt, or a script racing to submit against an already-authenticated
session could still call /exchange with an attacker-influenced `code`/`waba_id`
/`phone_number_id` and have it processed as if the legitimate owner had just
completed the Meta popup — a CSRF-shaped risk distinct from "is the caller
who they say they are." Binding a single-use, short-lived, server-minted
state value (minted immediately before the popup launches, consumed exactly
once when the callback is submitted) closes that gap the same way OAuth
`state` parameters always have.

Design:
  - POST /start (routes_whatsapp_connect.py) mints a token via
    mint_connect_state() and returns it to the frontend, which must hold it
    client-side across the Meta popup and submit it back unchanged in
    POST /exchange's `state` field.
  - Stored in Redis as `whatsapp_connect_state:<token>` -> JSON
    {"business_id": ..., "user_id": ...}, with a TTL
    (settings.WHATSAPP_CONNECT_STATE_TTL_SECONDS, default 600s/10min).
  - consume_connect_state() does an atomic GETDEL (single Redis round trip,
    single-use by construction — a second call for the same token, even by
    the legitimate business, always finds nothing) and checks the stored
    business_id against the CURRENT authenticated caller's business_id.
  - Redis unreachable at mint OR consume time -> StateServiceUnavailable is
    raised; callers must fail the request closed (503), never silently skip
    validation and let the exchange proceed unbound.
"""

import json
import logging
import secrets
import uuid
from dataclasses import dataclass

from redis.exceptions import RedisError

from backend.app.core.config import get_settings

logger = logging.getLogger("nextgen.whatsapp.connect_state")

_KEY_PREFIX = "whatsapp_connect_state:"


class StateServiceUnavailable(Exception):
    """Redis could not be reached to mint or validate a connect-state token."""


@dataclass(frozen=True)
class MintedState:
    token: str
    expires_in: int


async def mint_connect_state(redis, business_id: uuid.UUID, user_id: uuid.UUID) -> MintedState:
    """
    Mint a new single-use state token for `business_id`/`user_id` and store
    it in Redis with a TTL. Raises StateServiceUnavailable if Redis cannot be
    reached — callers must not mint a "state" that was never actually
    persisted anywhere, since that would make every later /exchange call
    for it fail as INVALID_OR_EXPIRED_STATE, or worse, silently be skipped.
    """
    settings = get_settings()
    ttl = settings.WHATSAPP_CONNECT_STATE_TTL_SECONDS
    token = secrets.token_urlsafe(32)
    payload = json.dumps({"business_id": str(business_id), "user_id": str(user_id)})
    try:
        await redis.set(_KEY_PREFIX + token, payload, ex=ttl)
    except RedisError as exc:
        logger.error("WHATSAPP_CONNECT_STATE_MINT_REDIS_ERROR: %s", exc)
        raise StateServiceUnavailable(
            "Could not reach the state store to start the WhatsApp connect flow."
        ) from exc
    return MintedState(token=token, expires_in=ttl)


async def consume_connect_state(redis, token: str, business_id: uuid.UUID) -> bool:
    """
    Atomically read-and-delete the state token (GETDEL — a single Redis
    round trip, so no separate GET+DELETE race window exists between two
    concurrent /exchange calls for the same token). Single-use by
    construction: whichever call reaches Redis first gets the value and
    deletes it in the same operation; every other caller (including a
    legitimate retry) gets None and must restart via POST /start.

    Returns True only when the token existed, had not expired, and its
    stored business_id matches the CURRENT authenticated caller's
    business_id. Returns False for a missing/expired token OR a business_id
    mismatch. In the mismatch case the token is still consumed (GETDEL
    already removed it) — this is deliberate: it prevents the same
    forged/stolen state value from being retried against the RIGHT
    business_id afterwards either.

    Raises StateServiceUnavailable if Redis cannot be reached — callers must
    reject the exchange request (503) rather than treat an unreachable
    Redis as "no state to check, proceed anyway".
    """
    try:
        raw = await redis.getdel(_KEY_PREFIX + token)
    except RedisError as exc:
        logger.error("WHATSAPP_CONNECT_STATE_CONSUME_REDIS_ERROR: %s", exc)
        raise StateServiceUnavailable(
            "Could not reach the state store to validate the WhatsApp connect flow."
        ) from exc

    if raw is None:
        return False

    try:
        data = json.loads(raw)
        stored_business_id = uuid.UUID(data["business_id"])
    except (ValueError, KeyError, TypeError) as exc:
        # Should be unreachable (we control the only writer), but never trust
        # a Redis value blindly — treat anything unparsable as invalid, not
        # as a crash.
        logger.error("WHATSAPP_CONNECT_STATE_CORRUPT: token_prefix=%s err=%s", token[:8], exc)
        return False

    return stored_business_id == business_id
