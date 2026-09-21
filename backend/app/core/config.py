"""
Application settings — loaded from .env.backend (or container env vars).

Model 1 Architecture:
  ONE Meta App → ONE WABA → ONE platform System User Access Token.
  Token lives here in env. NOT stored per-business in DB.
  Each business is identified only by its phone_number_id.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Environment allow-list ───────────────────────────────────────────────────
# ENVIRONMENT has NO default (see the Settings field below) — a deployment
# that forgets to set it must fail at boot, not silently fall back to
# "development" and skip every secret/signature check (this exact gap let a
# production deploy run unvalidated once — see KNOWN_BUGS.md BUG-0014).
# "test" exists only so the pytest suite (tests/conftest.py) can declare an
# explicit, intentional non-production value distinct from "development" —
# it is treated identically to "development" for secret-validation purposes.
VALID_ENVIRONMENTS = {"development", "staging", "production", "test"}


def _validate_environment_value(value: str) -> str:
    if value not in VALID_ENVIRONMENTS:
        raise ValueError(
            f"ENVIRONMENT must be one of {sorted(VALID_ENVIRONMENTS)}, got {value!r}. "
            "Every deployed service (backend, admin_api, worker) must set "
            "ENVIRONMENT explicitly — there is no default."
        )
    return value


def _looks_like_placeholder(value: str) -> bool:
    """
    Broader placeholder detection than an exact-string match.

    A real incident showed the gap in exact matching: JWT_SECRET_KEY was set
    to "CHANGE-ME-generate-a-strong-random-string" in production — clearly a
    never-rotated placeholder, but not equal to the one literal string this
    check used to look for ("CHANGE-ME-IN-PRODUCTION"), so it silently passed.
    Catch anything placeholder-shaped by substring/length instead of relying
    on one exact value.

    Shared by backend/app/main.py and admin_api/app/main.py so both planes
    apply the exact same placeholder-detection logic — see DECISIONS.md.
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


# Redis key the ARQ worker heartbeats to (see backend/app/worker.py) and that
# GET /ready reads to detect a hung/crashed worker process.
WORKER_HEARTBEAT_REDIS_KEY = "worker:heartbeat"


class Settings(BaseSettings):
    """Backend (data plane) settings."""

    model_config = SettingsConfigDict(
        env_file=".env.backend",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────
    APP_NAME: str = "NextGen Backend"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    # REQUIRED — no default. See VALID_ENVIRONMENTS above for why.
    ENVIRONMENT: str
    API_V1_PREFIX: str = "/v1"
    ALLOWED_ORIGINS: str = "https://nextgenintelligence.co.za,https://app.nextgenintelligence.co.za,https://staff.nextgenintelligence.co.za,https://admin.nextgenintelligence.co.za,http://localhost:3000,http://localhost:3001,http://localhost:5173,http://localhost:5174,http://localhost:5175"

    # ── Database ─────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nextgen"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 10

    # ── Redis ────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── JWT ───────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "CHANGE-ME-IN-PRODUCTION"
    # Separate signing secret for admin-plane (SUPER_ADMIN) tokens.
    # MUST differ from JWT_SECRET_KEY in production — a leak of the business
    # API secret must not allow forging admin tokens.
    JWT_ADMIN_SECRET_KEY: str = "CHANGE-ME-IN-PRODUCTION"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # STAFF tokens expire faster: a deactivated till user must lose access
    # within minutes, not half an hour.
    STAFF_ACCESS_TOKEN_EXPIRE_MINUTES: int = 10
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Credential encryption (payment provider keys at rest) ────────────
    # Fernet key — generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Empty = credentials stored as-is (development only; production startup
    # validation requires this to be set).
    CREDENTIALS_ENCRYPTION_KEY: str = ""
    # Optional comma-separated list of PREVIOUSLY-used Fernet keys — decrypt
    # only, never used to encrypt new values. Set this to the old primary key
    # when rotating CREDENTIALS_ENCRYPTION_KEY so already-encrypted rows
    # (payment credentials, and — Wave 0A/Milestone C — per-business WhatsApp
    # tokens on whatsapp_connections) keep decrypting. See
    # backend/app/core/crypto.py module docstring for the rotation model.
    CREDENTIALS_ENCRYPTION_KEYS_OLD: str = ""

    # ── Meta / WhatsApp (Model 1: single WABA, platform token) ───────────
    META_API_BASE_URL: str = "https://graph.facebook.com"
    META_API_VERSION: str = "v22.0"
    META_VERIFY_TOKEN: str = "CHANGE-ME"
    META_APP_SECRET: str = "CHANGE-ME"
    WHATSAPP_DEFAULT_ACCESS_TOKEN: str = "CHANGE-ME"
    WHATSAPP_WEBHOOK_PATH: str = "/v1/webhook/meta"

    # ── Meta / WhatsApp Embedded Signup (Wave 0A / Milestone C /
    #    DECISION-0022: customer-owned WhatsApp, additive to Model 1 above) ──
    # Both OPTIONAL — when either is unset, GET /business/whatsapp/connection/config
    # reports {"available": false, ...} rather than crashing boot. Public
    # identifiers only (safe to send to the frontend to launch FB.login);
    # the actual secret exchanged for a token is META_APP_SECRET above,
    # which never leaves the backend.
    META_APP_ID: str = ""
    META_ES_CONFIG_ID: str = ""

    # ── SaaS billing provider selection ──────────────────────────────────
    # Which BillingProvider (backend/app/billing/provider_base.py) actively
    # handles NextGen's own subscription billing of tenants. Stripe remains
    # fully implemented and is the default; PayFast is implemented but
    # unverified against live credentials (see KNOWN_BUGS.md). Switching is
    # a config change, never a code change — see backend/app/billing/registry.py.
    BILLING_PROVIDER: str = "STRIPE"  # STRIPE | PAYFAST

    # Set false to defer NextGen's own SaaS subscription billing entirely
    # (e.g. while a chosen provider's real merchant credentials aren't
    # configured yet) without weakening any OTHER production secret check.
    # Only skips the BILLING_PROVIDER-specific secret requirement in
    # _validate_production_secrets() — every other guard (JWT, credentials
    # encryption, Meta/WhatsApp) still applies in full. The billing routes
    # themselves (checkout/portal/cancel) are unaffected by this flag and
    # will simply error if invoked without real provider credentials, which
    # is the correct, honest behaviour while billing is deliberately deferred.
    BILLING_ENABLED: bool = True

    # NextGen's OWN PayFast merchant account for SaaS billing — distinct
    # from Business.payment_api_key/etc., which are each *tenant's own*
    # PayFast credentials for their customers' payments.
    PAYFAST_BILLING_MERCHANT_ID: str = ""
    PAYFAST_BILLING_MERCHANT_KEY: str = ""
    PAYFAST_BILLING_PASSPHRASE: str = ""

    # ── Stripe (NextGen's own SaaS subscription billing of tenants —
    #    distinct from the customer-facing payment providers below, which
    #    are how a tenant's own customers pay the tenant) ─────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    # One Stripe recurring Price ID per self-service plan tier. ENTERPRISE
    # is deliberately excluded — per PRODUCT_ROADMAP.md's packaging section,
    # Enterprise is sales-assisted/custom-contract, never self-service Checkout.
    STRIPE_PRICE_ID_STARTER: str = ""
    STRIPE_PRICE_ID_GROWTH: str = ""
    # Where Stripe Checkout/Billing Portal send the owner back to afterward.
    BILLING_SUCCESS_URL: str = "https://app.nextgenintelligence.co.za/settings/billing?checkout=success"
    BILLING_CANCEL_URL: str = "https://app.nextgenintelligence.co.za/settings/billing?checkout=cancelled"
    BILLING_PORTAL_RETURN_URL: str = "https://app.nextgenintelligence.co.za/settings/billing"

    # ── LLM ──────────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""
    LLM_DEFAULT_MODEL: str = "gpt-4o-mini"
    LLM_MAX_TOKENS: int = 500
    LLM_TEMPERATURE: float = 0.3

    # ── Supabase Storage (for assets) ────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_STORAGE_BUCKET: str = "nextgen-assets"

    # ── Payment providers ─────────────────────────────────────────────────
    # Public URL of this API — used by payment providers to POST webhooks back.
    # Must be the live, reachable API host. The old default here
    # (nextgen-api.onrender.com) went dead at some point and was never
    # updated — iKhoka/PayFast kept getting told to call back to a 503,
    # so a real customer payment succeeded on the provider's side but never
    # reached this backend to mark the order paid. Confirm this still
    # resolves (`curl <value>/health` should return 200) if it's ever
    # overridden via env var.
    BACKEND_PUBLIC_URL: str = "https://api.nextgenintelligence.co.za"
    # Where to redirect customers after a successful or cancelled payment.
    PAYMENT_RETURN_URL: str = "https://nextgenintelligence.co.za/payment/success"
    PAYMENT_CANCEL_URL: str = "https://nextgenintelligence.co.za/payment/cancelled"

    # ── Web Push (VAPID) ──────────────────────────────────────────────────
    # Generate keys once with:
    #   python -c "from py_vapid import Vapid; import base64; v=Vapid(); v.generate_keys();
    #     print('VAPID_PRIVATE_KEY=' + base64.b64encode(v.private_pem()).decode());
    #     print('VAPID_PUBLIC_KEY=' + v.public_key.serialize().decode())"
    # Store VAPID_PRIVATE_KEY as the base64-encoded PEM string.
    # Store VAPID_PUBLIC_KEY as the raw base64url string (sent to browsers as applicationServerKey).
    VAPID_PRIVATE_KEY: str = ""
    VAPID_PUBLIC_KEY: str = ""
    VAPID_CONTACT_EMAIL: str = "mailto:admin@nextgenintelligence.co.za"

    # ── Rate Limiting ────────────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    LOGIN_RATE_LIMIT_PER_MINUTE: int = 5
    PIN_RATE_LIMIT_PER_MINUTE: int = 10
    REFRESH_RATE_LIMIT_PER_MINUTE: int = 30
    ACCOUNT_LOCKOUT_ATTEMPTS: int = 5
    ACCOUNT_LOCKOUT_MINUTES: int = 30
    CUSTOMER_MESSAGE_RATE_PER_MINUTE: int = 10

    # ── Readiness ────────────────────────────────────────────────────────
    # GET /ready reports the ARQ worker's heartbeat as "stale" whenever it's
    # missing/expired. Whether that alone should flip the whole endpoint to
    # 503 is configurable: a deployment that truly has no worker process
    # (e.g. a POS-only install with WhatsApp processing intentionally not
    # running) can set this false. Default true — the worker is required by
    # every deployment today.
    READY_REQUIRES_WORKER: bool = True

    @field_validator("ENVIRONMENT")
    @classmethod
    def _check_environment(cls, value: str) -> str:
        return _validate_environment_value(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
