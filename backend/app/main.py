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

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.warning(
        "STARTUP: NextGen Backend v%s [env=%s] — "
        "webhook public at %s/webhook/meta — "
        "docs at /docs (debug=%s)",
        settings.APP_VERSION,
        settings.ENVIRONMENT,
        settings.API_V1_PREFIX,
        settings.DEBUG,
    )

    # Start outbox worker as background task
    import asyncio
    outbox_task = None
    try:
        from backend.app.bot.outbox_worker import run_outbox_worker
        outbox_task = asyncio.create_task(run_outbox_worker())
        logger.info("Outbox worker started as background task")
    except Exception:
        logger.warning("Failed to start outbox worker (non-fatal)")

    yield

    logger.info("Shutting down NextGen Backend...")
    if outbox_task:
        outbox_task.cancel()
        try:
            await outbox_task
        except asyncio.CancelledError:
            pass
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
