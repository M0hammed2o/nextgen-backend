"""
NextGen AI Platform — Backend (Data Plane)
FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core.config import WORKER_HEARTBEAT_REDIS_KEY, _looks_like_placeholder, get_settings
from backend.app.core.errors import UnhandledExceptionMiddleware, register_error_handlers
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

def _validate_production_secrets() -> None:
    """
    Crash immediately if placeholder secrets are present in a real deployment.

    Runs for every ENVIRONMENT except explicit "development"/"test" — staging
    is validated exactly like production. A misconfigured deployment is worse
    than a failed startup (see KNOWN_BUGS.md BUG-0014: ENVIRONMENT silently
    defaulting to "development" in production once disabled this entire
    check, Meta webhook HMAC verification, and Stripe webhook verification
    at once).
    """
    if settings.ENVIRONMENT in ("development", "test"):
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

    # NextGen's own SaaS billing provider (BILLING_PROVIDER: STRIPE | PAYFAST,
    # see backend/app/billing/registry.py / DECISION-0017) is a config choice,
    # not a code choice — so this check must only demand secrets for whichever
    # provider is actually active. Originally this unconditionally required
    # STRIPE_WEBHOOK_SECRET even when BILLING_PROVIDER=PAYFAST (found
    # 2026-07-27 when a Render deploy correctly refused to boot over a
    # missing Stripe secret while PayFast was the intended provider) — an
    # unverified webhook is the same real risk on either provider: a forged
    # event could flip any tenant's billing_status/is_active.
    #
    # BILLING_ENABLED=false deliberately skips this whole block — e.g. while
    # onboarding a real PayFast merchant account and not ready to accept
    # SaaS subscription payments yet. Every other secret above is still
    # enforced in full; this only ever relaxes the billing-webhook check,
    # never anything auth/tenant-isolation/WhatsApp-related.
    if settings.BILLING_ENABLED:
        if settings.BILLING_PROVIDER.upper() == "PAYFAST":
            if not settings.PAYFAST_BILLING_MERCHANT_ID or not settings.PAYFAST_BILLING_MERCHANT_KEY:
                errors.append(
                    "BILLING_PROVIDER=PAYFAST but PAYFAST_BILLING_MERCHANT_ID/"
                    "PAYFAST_BILLING_MERCHANT_KEY are not set — cannot create "
                    "checkout sessions"
                )
            if _looks_like_placeholder(settings.PAYFAST_BILLING_PASSPHRASE):
                errors.append(
                    "PAYFAST_BILLING_PASSPHRASE looks unset or placeholder — the "
                    "PayFast billing webhook (ITN) would accept unverified requests"
                )
        else:
            if _looks_like_placeholder(settings.STRIPE_WEBHOOK_SECRET):
                errors.append(
                    "STRIPE_WEBHOOK_SECRET looks unset or placeholder — the Stripe "
                    "billing webhook would accept unverified requests"
                )

    # Every OTHER webhook-dependent integration, generalised (BUG-0089): this
    # is the exact class of gap BUG-0001 exists to catch, and it recurred on
    # the Uber Eats webhook because the lesson wasn't generalised the first
    # time — UBER_EATS_CLIENT_SECRET/UBER_EATS_CLIENT_ID defaulted to "" and
    # were never added here. verify_signature() fails CLOSED on an empty
    # client secret (nothing forged is ever accepted), but that failure is
    # SILENT: every genuine, correctly-signed order is then logged as
    # BAD_SIGNATURE and acked HTTP 200 (Uber never retries), so a production
    # deploy missing this secret permanently discards every real paid order
    # with no boot-time signal at all. Add a new (enabled_flag, secret_value,
    # secret_name, consequence) tuple here for any FUTURE webhook-dependent
    # integration rather than writing a new bespoke if-block — that is the
    # actual fix for the "keeps recurring" problem, not just adding one more
    # line for Uber.
    _webhook_secret_requirements = (
        (
            settings.UBER_EATS_WEBHOOK_ENABLED,
            settings.UBER_EATS_CLIENT_SECRET,
            "UBER_EATS_CLIENT_SECRET",
            "Uber Eats webhook signatures cannot be verified — every genuine, "
            "correctly-signed order would be logged as BAD_SIGNATURE and "
            "silently discarded (200-acked, so Uber never retries, and no "
            "delivery_orders row is ever created for the watchdog to rescue)",
        ),
        (
            settings.UBER_EATS_WEBHOOK_ENABLED,
            settings.UBER_EATS_CLIENT_ID,
            "UBER_EATS_CLIENT_ID",
            "the platform-wide Uber Eats operational token cannot be minted — "
            "every accepted order's fetch/accept/deny call would fail",
        ),
    )
    for enabled, secret_value, secret_name, consequence in _webhook_secret_requirements:
        if enabled and _looks_like_placeholder(secret_value):
            errors.append(f"{secret_name} is not configured but its webhook is enabled — {consequence}")

    if errors:
        msg = (
            f"FATAL: {settings.ENVIRONMENT} deployment with unconfigured secrets:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )
        logger.critical(msg)
        raise RuntimeError(msg)


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

# ── Middleware ─────────────────────────────────────────────────────────────
# Order matters, but not "outermost first": Starlette's `add_middleware()`
# inserts at the front of its internal list, so the LAST middleware added
# here ends up OUTERMOST at runtime (empirically confirmed — see
# tests/test_error_handlers_cors.py). Runtime order for the four calls below
# is therefore: CORSMiddleware (outermost) -> UnhandledExceptionMiddleware ->
# CorrelationIDMiddleware -> RequestLoggingMiddleware (innermost, closest to
# routing). UnhandledExceptionMiddleware MUST stay added before
# CORSMiddleware (see its docstring in backend/app/core/errors.py) — moving
# it after CORSMiddleware would silently reopen BUG-0067's CORS-masking gap
# for any future unhandled exception.

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(UnhandledExceptionMiddleware)
# In development, ALSO allow any localhost port via regex. A static
# ALLOWED_ORIGINS list can never fully cover local frontend dev servers,
# whose Vite dev server silently falls back to the next free port whenever
# something else on the machine is already using its configured one (see
# KNOWN_BUGS.md BUG-0025/RELEASE_CHECKLIST.md) -- the Manager Dashboard
# landed on :8082 during local user-acceptance testing, which the static
# ALLOWED_ORIGINS list (added for BUG-0023) never anticipated, and the
# browser's login request failed with a generic "Failed to fetch" (a CORS
# rejection, not a real login/network failure) until this was added. Only
# takes effect when ENVIRONMENT=development -- production keeps the exact
# static allowlist, no regex fallback.
_cors_kwargs = {
    "allow_origins": settings.ALLOWED_ORIGINS.split(","),
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
if settings.ENVIRONMENT == "development":
    _cors_kwargs["allow_origin_regex"] = r"http://localhost:\d+"
app.add_middleware(CORSMiddleware, **_cors_kwargs)

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
from backend.app.api.v1.routes_delivery_webhook import router as delivery_webhook_router
from backend.app.api.v1.routes_business import router as business_router
from backend.app.api.v1.routes_menu import router as menu_router
from backend.app.api.v1.routes_orders import router as orders_router
from backend.app.api.v1.routes_pos import router as pos_router
from backend.app.api.v1.routes_external_orders import router as external_orders_router
from backend.app.api.v1.routes_delivery import router as delivery_router
from backend.app.api.v1.routes_import import router as import_router
from backend.app.api.v1.routes_setup import router as setup_router
from backend.app.api.v1.routes_inventory import router as inventory_router
from backend.app.api.v1.routes_recipes import router as recipes_router
from backend.app.api.v1.routes_receipts import digital_receipts_usage_router, router as receipts_router
from backend.app.api.v1.routes_receipt_public import router as receipt_public_router
from backend.app.api.v1.routes_stocktake import router as stocktake_router
from backend.app.api.v1.routes_wastage import router as wastage_router
from backend.app.api.v1.routes_billing import router as billing_settings_router
from backend.app.api.v1.routes_loyalty import router as loyalty_router
from backend.app.api.v1.routes_specials import router as specials_router
from backend.app.api.v1.routes_analytics import router as analytics_router
from backend.app.api.v1.routes_assets import router as assets_router
from backend.app.api.v1.routes_staff import router as staff_router
from backend.app.api.v1.routes_export import router as export_router
from backend.app.api.v1.routes_payments import router as payments_router
from backend.app.api.v1.routes_push import router as push_router
from backend.app.api.v1.routes_whatsapp_connect import router as whatsapp_connect_router
from backend.app.realtime.sse import router as sse_router
from backend.app.billing.stripe_webhooks import router as billing_router

PREFIX = settings.API_V1_PREFIX

app.include_router(auth_router, prefix=PREFIX)
app.include_router(webhook_router, prefix=PREFIX)
# Public, unauthenticated, no tenant component in the path — see
# routes_delivery_webhook.py's module docstring for the trust-boundary
# rationale (mirrors webhook_router's own public-router pattern above).
app.include_router(delivery_webhook_router, prefix=PREFIX)
app.include_router(business_router, prefix=PREFIX)
app.include_router(menu_router, prefix=PREFIX)
app.include_router(orders_router, prefix=PREFIX)
app.include_router(pos_router, prefix=PREFIX)
# Ciao Pizza P0 — inventory module. Every route below is additionally
# gated per tenant by Business.inventory_enabled, so registering them
# does not change behaviour for tenants without the module.
app.include_router(inventory_router, prefix=PREFIX)
app.include_router(import_router, prefix=PREFIX)
app.include_router(setup_router, prefix=PREFIX)
app.include_router(external_orders_router, prefix=PREFIX)
# Delivery marketplace business-plane API (list/manage connections, list
# delivery orders, menu mappings, manual accept/deny) — every route further
# gated per tenant by Business.delivery_platforms_enabled, same pattern as
# the inventory module above.
app.include_router(delivery_router, prefix=PREFIX)
app.include_router(recipes_router, prefix=PREFIX)
app.include_router(wastage_router, prefix=PREFIX)
app.include_router(stocktake_router, prefix=PREFIX)
app.include_router(receipts_router, prefix=PREFIX)
app.include_router(digital_receipts_usage_router, prefix=PREFIX)
# Public, UNAUTHENTICATED digital receipt page — GET /v1/r/{token}. Token
# alone is the entire lookup key (see routes_receipt_public.py's module
# docstring for why this is mounted under the standard /v1 prefix rather
# than an unprecedented un-prefixed route).
app.include_router(receipt_public_router, prefix=PREFIX)
app.include_router(billing_settings_router, prefix=PREFIX)
app.include_router(loyalty_router, prefix=PREFIX)
app.include_router(specials_router, prefix=PREFIX)
app.include_router(analytics_router, prefix=PREFIX)
app.include_router(assets_router, prefix=PREFIX)
app.include_router(staff_router, prefix=PREFIX)
app.include_router(export_router, prefix=PREFIX)
app.include_router(payments_router, prefix=PREFIX)
app.include_router(push_router, prefix=PREFIX)
# Wave 0A / Milestone C / DECISION-0022 — customer-owned WhatsApp via Meta
# Embedded Signup, additive alongside the existing platform-token webhook
# router above. META_APP_ID/META_ES_CONFIG_ID are optional (GET .../config
# reports available:false when unset) so registering this router never
# affects a deployment that hasn't configured Embedded Signup.
app.include_router(whatsapp_connect_router, prefix=PREFIX)
app.include_router(sse_router, prefix=PREFIX)
app.include_router(billing_router, prefix=PREFIX)


# ── Health / Readiness ───────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness check — app is running."""
    return {"status": "ok", "service": "backend", "version": settings.APP_VERSION}


@app.get("/ready")
async def readiness():
    """
    Readiness check — DB, Redis, and (if required) the ARQ worker's heartbeat.

    Returns HTTP 503 when DB or Redis is unreachable, or when the worker
    heartbeat is stale AND settings.READY_REQUIRES_WORKER is true (the
    default — a deployment with genuinely no worker process can opt out).

    Never returns exception text, hostnames, or credentials to the caller —
    only fixed strings ("ok"/"error"/"stale"). Full exception detail is
    logged server-side only.
    """
    from fastapi.responses import JSONResponse
    from sqlalchemy import text as sa_text

    from backend.app.db.session import engine, get_redis

    db_ok = False
    try:
        async with engine.connect() as conn:
            await conn.execute(sa_text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.exception("Readiness check: database unreachable")

    redis_ok = False
    redis_client = None
    try:
        redis_client = await get_redis()
        await redis_client.ping()
        redis_ok = True
    except Exception:
        logger.exception("Readiness check: redis unreachable")

    worker_status = "stale"
    if redis_ok:
        try:
            heartbeat = await redis_client.get(WORKER_HEARTBEAT_REDIS_KEY)
            worker_status = "ok" if heartbeat else "stale"
        except Exception:
            logger.exception("Readiness check: failed to read worker heartbeat")

    checks = {
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
        "worker": worker_status,
    }

    worker_blocks_ready = worker_status == "stale" and settings.READY_REQUIRES_WORKER
    all_ok = db_ok and redis_ok and not worker_blocks_ready

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "degraded", "checks": checks},
    )
