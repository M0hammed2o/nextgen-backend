"""
Server-Sent Events (SSE) endpoint for live order updates.
Staff/Manager/Owner can subscribe to real-time order events.
Uses Redis pubsub with fallback polling endpoint.
"""

import asyncio
import json
import logging
import uuid

import anyio
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.rbac import AuthUser
from backend.app.core.security import decode_access_token
from backend.app.db.session import get_db, get_redis

logger = logging.getLogger("nextgen.sse")
router = APIRouter(prefix="/business/orders", tags=["realtime"])

_bearer = HTTPBearer(auto_error=False)

_ALLOWED_SSE_ROLES = {"STAFF", "MANAGER", "OWNER"}


async def _get_sse_user(
    request: Request,
    token_param: str | None = Query(default=None, alias="token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> AuthUser:
    """
    Auth dependency for SSE endpoints.

    The browser's native EventSource API cannot set custom headers, so the
    frontend passes the JWT as ?token=<access_token>.  This dependency accepts
    the token from either source (header preferred, query param as fallback).
    """
    raw_token: str | None = None
    if credentials:
        raw_token = credentials.credentials
    elif token_param:
        raw_token = token_param

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_TOKEN", "message": "Authorization required"},
        )

    try:
        payload = decode_access_token(raw_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED", "message": "Access token has expired"},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "Invalid access token"},
        )

    bid_str = payload.get("bid")
    user = AuthUser(
        user_id=uuid.UUID(payload["sub"]),
        business_id=uuid.UUID(bid_str) if bid_str else None,
        role=payload["role"],
        token_type=payload.get("type", "access"),
    )

    if user.role not in _ALLOWED_SSE_ROLES or not user.business_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "INSUFFICIENT_ROLE", "message": "Staff or above required"},
        )

    # Same instant-cutoff check as backend/app/core/rbac.py::get_current_user
    # — without it, a suspended business or deactivated staff member could
    # keep a live order stream open until their JWT naturally expired.
    from shared.models.business import Business
    from shared.models.user import BusinessUser

    result = await db.execute(
        select(BusinessUser.is_active, Business.is_active, Business.suspended_reason)
        .join(Business, Business.id == BusinessUser.business_id)
        .where(BusinessUser.id == user.user_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_DISABLED", "message": "This account has been disabled"},
        )
    user_active, business_active, suspended_reason = row
    if not user_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ACCOUNT_DISABLED", "message": "This account has been disabled"},
        )
    if not business_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "BUSINESS_SUSPENDED",
                "message": "This business account has been suspended.",
                "details": {"reason": suspended_reason} if suspended_reason else None,
            },
        )

    return user


@router.get("/live/stream")
async def live_order_stream(
    request: Request,
    user: AuthUser = Depends(_get_sse_user),
):
    """
    SSE stream for live order updates.
    
    Events:
    - order_created: new order placed
    - order_status_changed: status transition
    
    Payload includes: order_id, order_number, status, total_cents, items summary.
    
    Client connects with:
        const es = new EventSource('/v1/business/orders/live/stream', {
            headers: { 'Authorization': 'Bearer <token>' }
        });
        es.onmessage = (event) => { ... };
    """
    business_id = str(user.business_id)

    async def event_generator():
        redis = await get_redis()
        pubsub = redis.pubsub()
        channel = f"orders:{business_id}"

        try:
            await pubsub.subscribe(channel)
            logger.info("SSE client connected: business=%s, user=%s", business_id, user.user_id)

            # Send initial heartbeat
            yield {"event": "connected", "data": json.dumps({"business_id": business_id})}

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message and message["type"] == "message":
                    yield {"event": "order_update", "data": message["data"]}
                else:
                    # Send keepalive every 15 seconds
                    yield {"event": "keepalive", "data": ""}
                    await asyncio.sleep(15)

        except asyncio.CancelledError:
            pass
        finally:
            # ── WHY THIS CLEANUP IS SHIELDED (KNOWN_BUGS.md BUG-0110) ───────
            # sse_starlette's EventSourceResponse drives this generator from
            # inside an anyio task group (sse_starlette.sse.EventSourceRespo
            # nse.__call__): on client disconnect it calls
            # `task_group.cancel_scope.cancel()`. Unlike a single
            # `asyncio.Task.cancel()`, anyio's structured-concurrency
            # cancellation is NOT one-shot — every checkpoint (every `await`)
            # reached while still inside that cancelled scope raises the
            # cancellation exception again, including ones in a `finally`
            # block. The previous version of this code awaited
            # `pubsub.unsubscribe(channel)` FIRST in this block; that await
            # was itself cancelled before it could complete, so
            # `pubsub.close()` on the next line never ran and the connection
            # was never returned to get_redis()'s bounded pool. Measured
            # production effect: exactly `max_connections` abandoned SSE
            # streams permanently exhausted the pool, `/ready` went 503
            # forever (no recovery short of a process restart), while
            # `/health` stayed 200 the whole time.
            #
            # `anyio.CancelScope(shield=True)` opens a scope that ignores the
            # *outer* cancellation, so every await inside it is guaranteed to
            # run to completion no matter how many times the outer scope
            # re-delivers CancelledError. `pubsub.aclose()` (the `.close()`
            # alias) is the call that actually returns the connection to the
            # pool — it disconnects the socket and calls
            # `connection_pool.release()` — so it is the one operation that
            # MUST complete. Unsubscribing first is not required for that:
            # closing the socket already ends the server-side subscription,
            # so it is now attempted best-effort, ordered ahead of close()
            # only because it's the "nicer" protocol citizen when it
            # succeeds, and its failure must never prevent close() from
            # running.
            with anyio.CancelScope(shield=True):
                try:
                    await pubsub.unsubscribe(channel)
                except Exception:
                    logger.debug(
                        "SSE pubsub unsubscribe failed during cleanup (non-fatal, "
                        "the connection is being closed regardless): business=%s",
                        business_id,
                        exc_info=True,
                    )
                try:
                    await pubsub.aclose()
                except Exception:
                    # Extremely unlikely (would require the underlying
                    # redis-py aclose() to itself fail, e.g. an already-broken
                    # socket) — logged loudly rather than silently swallowed,
                    # since redis-py's own aclose() skips returning the
                    # connection to the pool if this raises.
                    logger.warning(
                        "SSE pubsub close() raised while returning its redis "
                        "connection to the pool; the connection may remain "
                        "unavailable until reaped by health-check/TTL: business=%s",
                        business_id,
                        exc_info=True,
                    )
            logger.info("SSE client disconnected: business=%s", business_id)

    return EventSourceResponse(event_generator())
