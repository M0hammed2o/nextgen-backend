"""
NextGen AI Platform — Backend (Data Plane)
FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import get_settings
from backend.app.core.errors import register_error_handlers
from backend.app.core.middleware import CorrelationIDMiddleware, RequestLoggingMiddleware, setup_logging
from backend.app.db.session import close_db, close_redis

settings = get_settings()

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
setup_logging(settings.ENVIRONMENT)
logger = logging.getLogger("nextgen")


# ── Lifespan ─────────────────────────────────────────────────────────────────

def _looks_like_placeholder(value: str) -> bool:
    """
    Broader placeholder detection than an exact-string match.

    A real incident showed the gap in exact matching: JWT_SECRET_KEY was set
    to "CHANGE-ME-generate-a-strong-random-string" in production — clearly a
    never-rotated placeholder, but not equal to the one literal string this
    check used to look for ("CHANGE-ME-IN-PRODUCTION"), so it silently passed.
    Catch anything placeholder-shaped by substring/length instead of relying
    on one exact value.
    """
    if not value:
        return True
    lowered = value.lower()
    if any(marker in lowered for marker in ("change-me", "change_me", "your-", "your_", "example", "replace-me")):
        return True
    # A real secrets.token_urlsafe(64)/Fernet key is comfortably over 32 chars;
    # anything shorter is suspicious for a value that's supposed to be a
    # high-entropy signing key.
    if len(value) < 32:
        return True
    return False


def _validate_production_secrets() -> None:
    """
    Crash immediately if placeholder secrets are present in production.
    A misconfigured production deployment is worse than a failed startup.
    """
    if settings.ENVIRONMENT != "production":
        return

    _PLACEHOLDER_META = {"CHANGE-ME", "", "your-app-secret", "your-webhook-verify-token"}

    errors: list[str] = []
    if _looks_like_placeholder(settings.JWT_SECRET_KEY):
        errors.append("JWT_SECRET_KEY looks like a placeholder — all JWTs are forgeable")
    if _looks_like_placeholder(settings.JWT_ADMIN_SECRET_KEY):
        errors.append("JWT_ADMIN_SECRET_KEY looks like a placeholder — admin JWTs are forgeable")
    if settings.JWT_ADMIN_SECRET_KEY == settings.JWT_SECRET_KEY:
        errors.append("JWT_ADMIN_SECRET_KEY must differ from JWT_SECRET_KEY — planes must not share a signing key")
    if not settings.CREDENTIALS_ENCRYPTION_KEY:
        errors.append("CREDENTIALS_ENCRYPTION_KEY is not set — payment credentials would be stored in plaintext")
    if settings.META_APP_SECRET in _PLACEHOLDER_META:
        errors.append("META_APP_SECRET is set to a placeholder — webhook signatures cannot be verified")
    if settings.META_VERIFY_TOKEN in _PLACEHOLDER_META:
        errors.append("META_VERIFY_TOKEN is set to a placeholder — webhook registration will fail")
    if not settings.WHATSAPP_DEFAULT_ACCESS_TOKEN or settings.WHATSAPP_DEFAULT_ACCESS_TOKEN == "CHANGE-ME":
        errors.append("WHATSAPP_DEFAULT_ACCESS_TOKEN is not configured — cannot send WhatsApp messages")

    if errors:
        msg = "FATAL: Production deployment with unconfigured secrets:\n" + "\n".join(f"  • {e}" for e in errors)
        logger.critical(msg)
        raise RuntimeError(msg)

    # Stripe billing isn't in use yet (iKhoka is the active payment provider),
    # so this is a warning, not a startup-blocking error — see KNOWN_BUGS.md
    # BUG-0001. Promote this to the `errors` list above once Stripe billing
    # actually goes live, so an unset/placeholder webhook secret can never
    # silently ship again.
    if _looks_like_placeholder(settings.STRIPE_WEBHOOK_SECRET):
        logger.warning(
            "STRIPE_WEBHOOK_SECRET looks unset or placeholder — the Stripe "
            "billing webhook currently accepts unverified requests. Not "
            "fatal yet because Stripe billing isn't in use, but must be "
            "fixed before it is (see KNOWN_BUGS.md BUG-0001)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_production_secrets()

    logger.warning(
        "STARTUP: NextGen Backend v%s [env=%s] — "
        "webhook public at %s/webhook/meta — "
        "docs at /docs (debug=%s)",
        settings.APP_VERSION,
        settings.ENVIRONMENT,
        settings.API_V1_PREFIX,
        settings.DEBUG,
    )

    # Create ARQ pool — webhook handler enqueues jobs here.
    # Background processing (outbox, delivery fee timeout) runs in the
    # dedicated ARQ worker process: arq backend.app.worker.WorkerSettings
    from arq import create_pool
    from arq.connections import RedisSettings
    arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    app.state.arq_pool = arq_pool
    logger.info("ARQ pool ready — webhook jobs will be enqueued asynchronously.")

    yield

    logger.info("Shutting down NextGen Backend...")
    await arq_pool.aclose()
    await close_redis()
    await close_db()


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NextGen AI Platform — Backend",
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan,
)

# ── Middleware (order matters — outermost first) ─────────────────────────────

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Rate Limiting (slowapi — decorators live on the auth routes) ─────────────

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend.app.core.ratelimit import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Error Handlers ───────────────────────────────────────────────────────────

register_error_handlers(app)

# ── Routers (v1) ─────────────────────────────────────────────────────────────

from backend.app.api.v1.routes_auth import router as auth_router
from backend.app.api.v1.routes_webhook import router as webhook_router
from backend.app.api.v1.routes_business import router as business_router
from backend.app.api.v1.routes_menu import router as menu_router
from backend.app.api.v1.routes_orders import router as orders_router
from backend.app.api.v1.routes_specials import router as specials_router
from backend.app.api.v1.routes_analytics import router as analytics_router
from backend.app.api.v1.routes_assets import router as assets_router
from backend.app.api.v1.routes_staff import router as staff_router
from backend.app.api.v1.routes_export import router as export_router
from backend.app.api.v1.routes_payments import router as payments_router
from backend.app.api.v1.routes_push import router as push_router
from backend.app.realtime.sse import router as sse_router
from backend.app.billing.stripe_webhooks import router as billing_router

PREFIX = settings.API_V1_PREFIX

app.include_router(auth_router, prefix=PREFIX)
app.include_router(webhook_router, prefix=PREFIX)
app.include_router(business_router, prefix=PREFIX)
app.include_router(menu_router, prefix=PREFIX)
app.include_router(orders_router, prefix=PREFIX)
app.include_router(specials_router, prefix=PREFIX)
app.include_router(analytics_router, prefix=PREFIX)
app.include_router(assets_router, prefix=PREFIX)
app.include_router(staff_router, prefix=PREFIX)
app.include_router(export_router, prefix=PREFIX)
app.include_router(payments_router, prefix=PREFIX)
app.include_router(push_router, prefix=PREFIX)
app.include_router(sse_router, prefix=PREFIX)
app.include_router(billing_router, prefix=PREFIX)


# ── Health / Readiness ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness check — app is running."""
    return {"status": "ok", "service": "backend", "version": settings.APP_VERSION}


@app.get("/ready")
async def readiness():
    """Readiness check — DB and Redis are connected."""
    checks = {}
    try:
        from backend.app.db.session import engine
        async with engine.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    try:
        redis = await __import__("backend.app.db.session", fromlist=["get_redis"]).get_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", "checks": checks}
