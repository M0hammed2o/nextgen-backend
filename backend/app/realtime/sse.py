"""
Server-Sent Events (SSE) endpoint for live order updates.
Staff/Manager/Owner can subscribe to real-time order events.
Uses Redis pubsub with fallback polling endpoint.
"""

import asyncio
import json
import logging
import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sse_starlette.sse import EventSourceResponse
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
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            logger.info("SSE client disconnected: business=%s", business_id)

    return EventSourceResponse(event_generator())
