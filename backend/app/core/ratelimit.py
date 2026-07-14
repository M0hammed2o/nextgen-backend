"""
Shared rate limiter — slowapi backed by Redis.

Applied to authentication endpoints (login, PIN, staff directory, refresh)
where account lockout alone cannot stop volumetric abuse. Webhook endpoints
are deliberately NOT throttled — they are signature-verified and throttling
risks dropping legitimate Meta/payment provider bursts.

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
