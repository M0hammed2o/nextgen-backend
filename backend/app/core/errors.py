"""
Standardized error responses.

All API errors follow this format:
{
    "error": {
        "code": "BUSINESS_SUSPENDED",
        "message": "This business account has been suspended.",
        "details": {}
    }
}
"""

import logging
import re

from fastapi import FastAPI, Request, status
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse

logger = logging.getLogger("nextgen.errors")


def _unhandled_exception_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred. Please try again or contact support.",
                "details": None,
            }
        },
    )


class UnhandledExceptionMiddleware(BaseHTTPMiddleware):
    """
    Catches any exception that has no more specific registered handler
    (`AppError`/`RequestValidationError`/`HTTPException` are already
    resolved to a `Response` by Starlette's `ExceptionMiddleware` before
    this middleware ever sees them) — e.g. `FPDFUnicodeEncodingException`
    from `backend/app/billing/commercial_invoice_pdf.py` (BUG-0067).

    MUST be registered with `app.add_middleware()` BEFORE `CORSMiddleware`
    (i.e. earlier in the call sequence — Starlette's `add_middleware` makes
    the LAST-added middleware outermost, so adding this one first keeps it
    INSIDE `CORSMiddleware`). This is not just style: registering a catch-all
    via `@app.exception_handler(Exception)` alone does NOT fix the CORS gap,
    because Starlette special-cases the literal `Exception`/500 key — it
    routes that handler to `ServerErrorMiddleware`, which sits OUTSIDE every
    `add_middleware()` layer including CORS, so the resulting response still
    never passes through CORS's header injection. Catching the exception
    here, inside CORSMiddleware, and returning a normal `Response` is what
    actually gets Access-Control-Allow-Origin onto a genuine 500 — without
    it, the browser reports a misleading "blocked by CORS policy" error
    instead of the real failure (BUG-0067's secondary symptom).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> StarletteResponse:
        try:
            return await call_next(request)
        except Exception:
            logger.exception(
                "Unhandled exception on %s %s", request.method, request.url.path
            )
            return _unhandled_exception_response()


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class AppError(Exception):
    """Base application error that maps to a structured API response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


# ── Common errors ────────────────────────────────────────────────────────────

_NOT_FOUND_CODE_SEPARATOR_RE = re.compile(r"[\s\-]+")
_NOT_FOUND_CODE_MULTI_UNDERSCORE_RE = re.compile(r"_{2,}")


def _normalize_not_found_code(resource: str) -> str:
    """
    Build the machine-readable prefix of a NOT_FOUND code from a
    human-readable resource name (BUG-0113).

    `NotFoundError` used to build its `code` as `f"{resource.upper()}_NOT_FOUND"`
    with no normalisation, so any resource string containing a space or
    hyphen produced a malformed code (e.g. "Menu item" -> "MENU ITEM_NOT_FOUND",
    "Menu add-on" -> "MENU ADD-ON_NOT_FOUND"). This collapses any run of
    whitespace and/or hyphens to a single underscore, then collapses any
    resulting run of underscores to one and strips leading/trailing
    underscores, so "Menu item", "Menu-item" and "menu   item" all resolve
    to the same MENU_ITEM code.

    Deliberately CamelCase-preserving: word boundaries inside a single
    unbroken token (e.g. "LoyaltyProgram") are NOT split here — only
    whitespace/hyphen separators are normalised. This is intentional, not
    an oversight: "LoyaltyProgram" must keep producing the exact
    LOYALTYPROGRAM_NOT_FOUND code, which four live nextgen-staff-v3
    consumers (`LoyaltyBalance.tsx`, `lib/loyalty.ts`, `types/index.ts`)
    depend on to detect "this business has no loyalty programme" cleanly.
    Splitting it into LOYALTY_PROGRAM_NOT_FOUND would silently break that
    (the exact class of regression already fixed once as BUG-0108). See
    DECISIONS.md for the recorded carve-out.
    """
    normalized = _NOT_FOUND_CODE_SEPARATOR_RE.sub("_", resource.strip()).upper()
    normalized = _NOT_FOUND_CODE_MULTI_UNDERSCORE_RE.sub("_", normalized)
    return normalized.strip("_")


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str | None = None):
        super().__init__(
            code=f"{_normalize_not_found_code(resource)}_NOT_FOUND",
            message=f"{resource} not found" + (f": {resource_id}" if resource_id else ""),
            status_code=404,
        )


class BusinessSuspendedError(AppError):
    def __init__(self, reason: str | None = None):
        super().__init__(
            code="BUSINESS_SUSPENDED",
            message="This business account has been suspended.",
            status_code=403,
            details={"reason": reason} if reason else None,
        )


class RateLimitError(AppError):
    def __init__(self, limit_type: str):
        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message=f"Rate limit exceeded: {limit_type}",
            status_code=429,
        )


class DailyLimitError(AppError):
    def __init__(self, limit_type: str, limit: int):
        super().__init__(
            code="DAILY_LIMIT_EXCEEDED",
            message=f"Daily {limit_type} limit reached ({limit})",
            status_code=429,
            details={"limit_type": limit_type, "limit": limit},
        )


class InvalidTransitionError(AppError):
    def __init__(self, current: str, requested: str, detail: str | None = None):
        super().__init__(
            code="INVALID_STATUS_TRANSITION",
            message=detail or f"Cannot transition from {current} to {requested}",
            status_code=422,
            details={"current_status": current, "requested_status": requested},
        )


class DuplicateError(AppError):
    def __init__(self, resource: str, field: str):
        # Same normalisation as NotFoundError, for the same reason (BUG-0113):
        # this class had the identical unnormalised-code defect, emitting
        # "ADMIN USER_DUPLICATE" and "BUSINESS FIELD_DUPLICATE" — malformed
        # machine-readable codes with a literal space. Verified zero frontend
        # or e2e consumers of any *_DUPLICATE code from this class before
        # changing it (the only *_DUPLICATE code any frontend matches on is
        # OPENING_STOCK_ALREADY_SET, which is a distinct hand-written code and
        # is unaffected). The human-readable message is deliberately left
        # building from the original `resource` string so it still reads
        # naturally ("A business field with this slug already exists").
        super().__init__(
            code=f"{_normalize_not_found_code(resource)}_DUPLICATE",
            message=f"A {resource} with this {field} already exists",
            status_code=409,
        )


# ── Exception handlers (register on FastAPI app) ────────────────────────────

def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # exc.errors() is NOT guaranteed to be JSON-serialisable. When a
        # Pydantic `field_validator` rejects a value by raising ValueError,
        # Pydantic puts the exception OBJECT itself into the entry's `ctx`,
        # and json.dumps then raises inside this handler — turning what
        # should be a clean 422 into an unhandled 500 with no useful body.
        #
        # Reproduced with the long-standing receipt_paper_width_mm validator,
        # so this affects every custom validator in the codebase, not just
        # recent ones. jsonable_encoder walks the structure and stringifies
        # anything it cannot represent, which keeps the message a client
        # needs while guaranteeing the response can actually be sent.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "details": {
                        "errors": jsonable_encoder(
                            exc.errors(), custom_encoder={Exception: str}
                        )
                    },
                }
            },
        )

    async def _redis_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        BUG-0078. A Redis AVAILABILITY failure is a 503, not a 500.

        The specific case this exists for: the slowapi rate limiter's own
        Redis storage is consulted by the @limiter decorator BEFORE the route
        body runs, so a route that deliberately handles Redis being down
        (routes_whatsapp_connect.py's /start and /exchange translate
        StateServiceUnavailable into a 503) never got the chance — the
        limiter's raw redis.exceptions.ConnectionError escaped first as an
        unhandled 500. The fail-CLOSED behaviour was always correct (no Graph
        call, no DB mutation); only the status code was wrong.

        Deliberately narrow: ONLY ConnectionError/TimeoutError, which mean
        "the dependency is unreachable". Every other redis.exceptions.*
        (ResponseError, DataError, ...) indicates a malformed command — our
        bug, not an outage — and must keep surfacing as a 500 rather than
        being dressed up as a transient outage a client should retry.

        The exception text is deliberately NOT echoed: it embeds the Redis
        host/port, and a REDIS_URL can carry a password.
        """
        logger.error(
            "Redis unavailable (%s) on %s %s — returning 503",
            exc.__class__.__name__, request.method, request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_TEMPORARILY_UNAVAILABLE",
                    "message": (
                        "This service is temporarily unavailable — please try again "
                        "shortly."
                    ),
                    "details": None,
                }
            },
        )

    app.add_exception_handler(RedisConnectionError, _redis_unavailable_handler)
    app.add_exception_handler(RedisTimeoutError, _redis_unavailable_handler)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Secondary, last-resort safety net only — Starlette routes a
        # registered `Exception` handler to `ServerErrorMiddleware`, which is
        # OUTSIDE CORSMiddleware, so a response built here alone would still
        # be missing CORS headers. The actual CORS-preserving fix is
        # `UnhandledExceptionMiddleware` above, which MUST be registered via
        # `app.add_middleware()` before `CORSMiddleware` in main.py. This
        # handler only fires if something escapes even that middleware (e.g.
        # an exception raised by a middleware layer added outside it) — it
        # still guarantees a structured JSON body instead of Starlette's
        # bare-text debug/500 fallback, just without a CORS-header guarantee.
        logger.exception(
            "Unhandled exception reached the last-resort handler on %s %s",
            request.method, request.url.path,
        )
        return _unhandled_exception_response()
