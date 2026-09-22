"""
Shared rate limiter — slowapi backed by Redis.

Applied to authentication endpoints (login, PIN, staff directory, refresh)
where account lockout alone cannot stop volumetric abuse. The Meta and
payment-provider webhook endpoints are deliberately NOT throttled — they are
signature-verified and throttling risks dropping legitimate provider bursts.

The delivery-marketplace webhook (`POST /v1/webhook/delivery/{provider}`,
`routes_delivery_webhook.py`) is the one exception: it IS throttled, via
`limiter.shared_limit(..., scope="delivery_webhook")` — a single fixed-scope
bucket per client IP shared across every `{provider}` value (see BUG-0077 and
that route's own module docstring for why `scope=` specifically, not
`limiter.limit()`, is required for a route with a path parameter).

The limiter keys on the client IP. For this to be the REAL client IP behind
Render's proxy, uvicorn must run with --proxy-headers (see start.sh).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.app.core.config import get_settings

settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.REDIS_URL,
    enabled=settings.RATE_LIMIT_ENABLED,
)
