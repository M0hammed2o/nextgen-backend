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


@asynccontextmanager
async def lifespan(app: FastAPI):
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

# ── Routers ──────────────────────────────────────────────────────────────────
from admin_api.app.api.v1.routes_auth import router as admin_auth_router
from admin_api.app.api.v1.routes_businesses import router as admin_biz_router
from admin_api.app.api.v1.routes_usage import router as admin_usage_router

PREFIX = settings.API_V1_PREFIX  # e.g. "/v1/admin"

app.include_router(admin_auth_router, prefix=PREFIX)
app.include_router(admin_biz_router, prefix=PREFIX)
app.include_router(admin_usage_router, prefix=PREFIX)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "admin_api", "version": settings.APP_VERSION}