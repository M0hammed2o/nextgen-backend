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
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

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

    # ── Rate Limiting ────────────────────────────────────────────────────
    LOGIN_RATE_LIMIT_PER_MINUTE: int = 5
    ACCOUNT_LOCKOUT_ATTEMPTS: int = 5
    ACCOUNT_LOCKOUT_MINUTES: int = 30
    CUSTOMER_MESSAGE_RATE_PER_MINUTE: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
