"""
NextGen AI Platform — Admin API (Control Plane)
Super Admin dashboard backend.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api.app.core.config import get_admin_settings
from backend.app.core.config import _looks_like_placeholder, _points_at_loopback_or_is_invalid
from backend.app.core.errors import UnhandledExceptionMiddleware, register_error_handlers
from backend.app.core.middleware import CorrelationIDMiddleware, RequestLoggingMiddleware, setup_logging
from backend.app.db.session import close_db, close_redis

settings = get_admin_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
setup_logging(settings.ENVIRONMENT)
logger = logging.getLogger("nextgen.admin")


def _validate_production_secrets() -> None:
    """
    Crash immediately if placeholder secrets are present in a real
    deployment. Admin tokens are signed by backend.app.core.security using
    the BACKEND settings object, so those are the values that must be
    validated here.

    Runs for every ENVIRONMENT except explicit "development"/"test" — staging
    is validated exactly like production, matching backend/app/main.py.
    """
    from backend.app.core.config import get_settings as get_backend_settings

    backend_settings = get_backend_settings()
    if backend_settings.ENVIRONMENT in ("development", "test"):
        return

    errors: list[str] = []
    if _looks_like_placeholder(backend_settings.JWT_SECRET_KEY):
        errors.append("JWT_SECRET_KEY looks like a placeholder — all JWTs are forgeable")
    if _looks_like_placeholder(backend_settings.JWT_ADMIN_SECRET_KEY):
        errors.append("JWT_ADMIN_SECRET_KEY looks like a placeholder — admin JWTs are forgeable")
    if backend_settings.JWT_ADMIN_SECRET_KEY == backend_settings.JWT_SECRET_KEY:
        errors.append("JWT_ADMIN_SECRET_KEY must differ from JWT_SECRET_KEY — planes must not share a signing key")

    # BUG-0085: the SQLAlchemy engine admin_api uses (backend.app.db.session
    # .engine) is a single shared object constructed from BACKEND settings,
    # not admin_api's own — so this process must validate it too, the same
    # reason the JWT checks above read backend_settings instead of trusting
    # backend/app/main.py's own lifespan to have already run (it doesn't run
    # in this process at all).
    if backend_settings.SQL_ECHO_ENABLED:
        errors.append(
            "SQL_ECHO_ENABLED is true — SQLAlchemy would log every statement's "
            "bound parameters verbatim, including real customer PII such as "
            "DigitalReceipt.phone_number_e164 and Customer.wa_id/phone_number"
        )

    # Same reasoning as SQL_ECHO_ENABLED immediately above: admin_api is a
    # separate deployable from backend/app/main.py, with no guarantee the
    # backend process's own startup check ever ran anywhere near it, yet it
    # imports and uses the exact same shared engine/redis pool — built from
    # backend_settings.DATABASE_URL/REDIS_URL, never from AdminSettings'
    # own same-named (and, confirmed in-session, otherwise-unused) fields.
    # A hardening entry alongside BUG-0115 records the backend-side half of
    # this check; this is the "checked one deployable but not its sibling"
    # gap this repo has hit before, closed here for both at once.
    for _url_value, _var_name, _consequence in (
        (
            backend_settings.DATABASE_URL,
            "DATABASE_URL",
            "the database would be unreachable (or silently point at a "
            "local/loopback Postgres instead of the real one)",
        ),
        (
            backend_settings.REDIS_URL,
            "REDIS_URL",
            "rate limiting and idempotency keys would be unreachable",
        ),
    ):
        if _points_at_loopback_or_is_invalid(_url_value):
            errors.append(
                f"{_var_name} is missing, unparseable, or still points at "
                f"localhost/loopback — {_consequence}. (Value withheld from "
                f"this message — {_var_name} carries credentials.)"
            )

    if errors:
        msg = (
            f"FATAL: Admin API {backend_settings.ENVIRONMENT} deployment with unconfigured secrets:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )
        logger.critical(msg)
        raise RuntimeError(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_production_secrets()
    logger.info("Starting NextGen Admin API v%s", settings.APP_VERSION)
    yield
    await close_redis()
    await close_db()


app = FastAPI(
    title="NextGen AI Platform — Admin API",
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.DEBUG else None,
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(CorrelationIDMiddleware)
# UnhandledExceptionMiddleware MUST be added before CORSMiddleware — see its
# docstring in backend/app/core/errors.py and the matching comment in
# backend/app/main.py (Starlette's add_middleware() makes the LAST-added
# middleware outermost, so this keeps it INSIDE CORSMiddleware, which is
# what actually gets CORS headers onto an unhandled-exception 500).
app.add_middleware(UnhandledExceptionMiddleware)
# See backend/app/main.py's matching comment: a static ALLOWED_ORIGINS list
# can't cover Vite's automatic port-fallback for local frontend dev servers.
# Development-only; production keeps the static allowlist only.
_cors_kwargs = {
    "allow_origins": settings.ALLOWED_ORIGINS.split(","),
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
if settings.ENVIRONMENT == "development":
    _cors_kwargs["allow_origin_regex"] = r"http://localhost:\d+"
app.add_middleware(CORSMiddleware, **_cors_kwargs)

register_error_handlers(app)

# ── Rate Limiting (slowapi — decorator lives on the admin login route) ───────

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend.app.core.ratelimit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Routers ──────────────────────────────────────────────────────────────────
from admin_api.app.api.v1.routes_admin_users import router as admin_admin_users_router
from admin_api.app.api.v1.routes_ai_emails import router as admin_ai_emails_router
from admin_api.app.api.v1.routes_auth import router as admin_auth_router
from admin_api.app.api.v1.routes_businesses import router as admin_biz_router
from admin_api.app.api.v1.routes_commercial import router as admin_commercial_router
from admin_api.app.api.v1.routes_usage import router as admin_usage_router

PREFIX = settings.API_V1_PREFIX  # e.g. "/v1/admin"

app.include_router(admin_auth_router, prefix=PREFIX)
app.include_router(admin_biz_router, prefix=PREFIX)
app.include_router(admin_commercial_router, prefix=PREFIX)
app.include_router(admin_usage_router, prefix=PREFIX)
app.include_router(admin_ai_emails_router, prefix=PREFIX)
app.include_router(admin_admin_users_router, prefix=PREFIX)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "admin_api", "version": settings.APP_VERSION}