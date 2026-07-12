"""
NextGen AI Platform — Admin API (Control Plane)
Super Admin dashboard backend.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from admin_api.app.core.config import get_admin_settings
from backend.app.core.errors import register_error_handlers
from backend.app.core.middleware import CorrelationIDMiddleware, RequestLoggingMiddleware
from backend.app.db.session import close_db, close_redis

settings = get_admin_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("nextgen.admin")


def _validate_production_secrets() -> None:
    """
    Crash immediately if placeholder secrets are present in production.
    Admin tokens are signed by backend.app.core.security using the BACKEND
    settings object, so those are the values that must be validated here.
    """
    from backend.app.core.config import get_settings as get_backend_settings

    backend_settings = get_backend_settings()
    if backend_settings.ENVIRONMENT != "production":
        return

    _PLACEHOLDER_JWT = "CHANGE-ME-IN-PRODUCTION"
    errors: list[str] = []
    if backend_settings.JWT_SECRET_KEY == _PLACEHOLDER_JWT:
        errors.append("JWT_SECRET_KEY is set to the default placeholder — all JWTs are forgeable")
    if backend_settings.JWT_ADMIN_SECRET_KEY == _PLACEHOLDER_JWT:
        errors.append("JWT_ADMIN_SECRET_KEY is set to the default placeholder — admin JWTs are forgeable")
    if backend_settings.JWT_ADMIN_SECRET_KEY == backend_settings.JWT_SECRET_KEY:
        errors.append("JWT_ADMIN_SECRET_KEY must differ from JWT_SECRET_KEY — planes must not share a signing key")

    if errors:
        msg = "FATAL: Admin API production deployment with unconfigured secrets:\n" + "\n".join(
            f"  • {e}" for e in errors
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)

# ── Rate Limiting (slowapi — decorator lives on the admin login route) ───────

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend.app.core.ratelimit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Routers ──────────────────────────────────────────────────────────────────
from admin_api.app.api.v1.routes_ai_emails import router as admin_ai_emails_router
from admin_api.app.api.v1.routes_auth import router as admin_auth_router
from admin_api.app.api.v1.routes_businesses import router as admin_biz_router
from admin_api.app.api.v1.routes_usage import router as admin_usage_router

PREFIX = settings.API_V1_PREFIX  # e.g. "/v1/admin"

app.include_router(admin_auth_router, prefix=PREFIX)
app.include_router(admin_biz_router, prefix=PREFIX)
app.include_router(admin_usage_router, prefix=PREFIX)
app.include_router(admin_ai_emails_router, prefix=PREFIX)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "admin_api", "version": settings.APP_VERSION}