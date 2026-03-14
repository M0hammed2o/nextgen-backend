"""
Server-Sent Events (SSE) endpoint for live order updates.
Staff/Manager/Owner can subscribe to real-time order events.
Uses Redis pubsub with fallback polling endpoint.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.rbac import AuthUser, require_staff_or_above
from backend.app.db.session import get_db, get_redis

logger = logging.getLogger("nextgen.sse")
router = APIRouter(prefix="/business/orders", tags=["realtime"])


@router.get("/live/stream")
async def live_order_stream(
    request: Request,
    user: AuthUser = Depends(require_staff_or_above),
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
