"""
Middleware — correlation IDs, request logging, timing, token masking.
Every request gets a correlation_id (from header or generated).
Propagated through logs and into audit_events.

Model 1 addition: TokenMaskingFilter masks access tokens in log output.
"""

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("nextgen")

# Context var for correlation ID — accessible anywhere in the request lifecycle
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

# Regex to find Bearer tokens in log output
_BEARER_PATTERN = re.compile(
    r"(Bearer\s+)([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)
_GENERIC_TOKEN_PATTERN = re.compile(
    r"(token[\"':\s=]+)([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)


class TokenMaskingFilter(logging.Filter):
    """
    Log filter that masks access tokens before they reach log output.
    Prevents accidental token leakage in production logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _BEARER_PATTERN.sub(
                lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
                record.msg,
            )
            record.msg = _GENERIC_TOKEN_PATTERN.sub(
                lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
                record.msg,
            )
        # Also check args (for %-style formatting)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _mask_string(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _mask_string(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


def _mask_string(s: str) -> str:
    """Mask any token patterns found in a string."""
    s = _BEARER_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
        s,
    )
    return s


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Assigns a correlation ID to every request.
    - Uses X-Correlation-ID header if provided by the client.
    - Otherwise generates a new UUID.
    - Sets it on response headers for tracing.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())[:16]
        correlation_id_ctx.set(cid)
        request.state.correlation_id = cid

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with method, path, status, and duration.
    Uses structured logging format.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        cid = getattr(request.state, "correlation_id", "unknown")

        # Skip logging health checks to reduce noise
        if request.url.path not in ("/health", "/ready"):
            logger.info(
                "request_completed",
                extra={
                    "correlation_id": cid,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "client_ip": request.client.host if request.client else None,
                },
            )

        return response


def get_correlation_id() -> str:
    """Get the current request's correlation ID (for use in services/audit)."""
    return correlation_id_ctx.get()


def setup_logging(environment: str = "development") -> None:
    """
    Configure logging with token masking filter.
    Call this during app startup.
    """
    mask_filter = TokenMaskingFilter()

    # Apply to root logger so ALL log output is masked
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(mask_filter)

    # Also apply to our app loggers
    for name in ("nextgen", "nextgen.webhook", "nextgen.bot", "nextgen.outbox"):
        logging.getLogger(name).addFilter(mask_filter)
