"""
Application settings — loaded from .env.backend (or container env vars).

Model 1 Architecture:
  ONE Meta App → ONE WABA → ONE platform System User Access Token.
  Token lives here in env. NOT stored per-business in DB.
  Each business is identified only by its phone_number_id.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    ENVIRONMENT: str = "development"  # development | staging | production
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

    # ── Meta / WhatsApp (Model 1: single WABA, platform token) ───────────
    META_API_BASE_URL: str = "https://graph.facebook.com"
    META_API_VERSION: str = "v22.0"
    META_VERIFY_TOKEN: str = "CHANGE-ME"
    META_APP_SECRET: str = "CHANGE-ME"
    WHATSAPP_DEFAULT_ACCESS_TOKEN: str = "CHANGE-ME"
    WHATSAPP_WEBHOOK_PATH: str = "/v1/webhook/meta"

    # ── Stripe ───────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
