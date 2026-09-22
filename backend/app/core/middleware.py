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


# BUG-0075. Credential-bearing QUERY PARAMETERS, which neither pattern above
# matches: _BEARER_PATTERN needs a literal "Bearer ", and _GENERIC_TOKEN_
# PATTERN needs the substring "token" — absent from "client_secret=" entirely,
# and followed by "?" rather than a quote/colon/space/equals in
# "access_token?client_id=".
#
# Masked COMPLETELY rather than in the partial first6...last4 style used for
# bearer tokens above: META_APP_SECRET is also the HMAC key for Meta webhook
# signature verification, so revealing any part of it is a platform-wide
# forgery risk, and an OAuth authorization code is a single-use credential
# that is not useful to see partially. The parameter NAME is kept so the log
# line stays diagnostic.
_SECRET_PARAM_PATTERN = re.compile(
    r"\b(client_secret|access_token|refresh_token|api_key|apikey|password)="
    r'([^&\s"\']+)',
    re.IGNORECASE,
)

# An OAuth authorization code. Length-gated, and deliberately does NOT match
# "error_code="/"error_subcode="/"status_code=" (no word boundary before
# "code" there), so Meta's short numeric diagnostic codes — code=190,
# code=100 — stay readable, since in those messages they are the whole point.
_OAUTH_CODE_PATTERN = re.compile(
    r"\b(code)=([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)

_REDACTED = "***"

# Third-party loggers that emit credential-bearing URLs — the ACTUAL emitter
# of the leak in BUG-0075 is httpx, not any call site in this codebase.
_THIRD_PARTY_CREDENTIAL_LOGGERS = ("httpx",)


def _mask_string(s: str) -> str:
    """Mask every credential pattern this filter knows about."""
    s = _BEARER_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
        s,
    )
    s = _GENERIC_TOKEN_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
        s,
    )
    s = _SECRET_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", s)
    s = _OAUTH_CODE_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", s)
    return s


def _mask_arg(a):
    """
    Mask one %-style log argument.

    Non-`str` arguments used to be skipped outright, which made this filter a
    complete no-op against the leak it most needed to catch: httpx logs
    `HTTP Request: %s %s ...` with `request.url` as an `httpx.URL` OBJECT, so
    the full code-exchange query string — client_secret and all — sailed
    straight through (BUG-0075).

    A non-str arg is therefore stringified and masked, but the result is only
    substituted back when masking ACTUALLY changed something. That keeps
    %d/%f/%03d formatting working for ordinary ints/floats, which would
    otherwise raise at render time.
    """
    if isinstance(a, str):
        return _mask_string(a)
    if a is None or isinstance(a, (int, float, bool)):
        return a
    try:
        text = str(a)
    except Exception:  # noqa: BLE001 — never let logging raise
        return a
    masked = _mask_string(text)
    return masked if masked != text else a


class TokenMaskingFilter(logging.Filter):
    """
    Log filter that masks access tokens and other credentials before they
    reach log output. Prevents accidental credential leakage in production
    logs.

    Attached in setup_logging() to the root logger's handlers, to this app's
    own loggers, and to third-party loggers known to emit credential-bearing
    URLs (httpx, uvicorn.access). The latter matter because those loggers are
    the actual emitters, and a filter attached to a LOGGER runs inside
    Logger.handle() for every record logged through it, regardless of where
    the handlers live.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _mask_string(record.msg)
        # Also check args (for %-style formatting)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _mask_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_mask_arg(a) for a in record.args)
            else:
                record.args = _mask_arg(record.args)
        return True


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

    # BUG-0075: httpx logs every request as
    # `HTTP Request: GET <full url> "HTTP/1.1 200 OK"` at INFO, and the Meta
    # code-exchange URL legitimately carries client_secret/code as query
    # parameters (that is how Meta's /oauth/access_token endpoint works, so
    # the request shape itself cannot change). Attach the filter to the
    # EMITTING logger rather than relying on a root handler being configured.
    for name in _THIRD_PARTY_CREDENTIAL_LOGGERS:
        logger_obj = logging.getLogger(name)
        if not any(isinstance(f, TokenMaskingFilter) for f in logger_obj.filters):
            logger_obj.addFilter(mask_filter)
        # These loggers commonly set propagate=False and carry their own
        # handlers, which a root-handler filter would never reach.
        for handler in logger_obj.handlers:
            handler.addFilter(mask_filter)
