# admin_api/app/core/config.py
"""
Admin API settings (control plane).
Uses same DB/Redis/JWT as backend.
Loads env from .env.admin
"""

from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.core.config import _validate_environment_value


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.admin",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "NextGen Admin API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    # REQUIRED — no default. Same allow-list/rationale as backend's Settings
    # (backend/app/core/config.py) — a missing ENVIRONMENT must fail at boot,
    # not silently behave as "development".
    ENVIRONMENT: str
    API_V1_PREFIX: str = "/v1"
    ALLOWED_ORIGINS: str = "https://admin.nextgenintelligence.co.za,http://localhost:3000,http://localhost:3001,http://localhost:3002,http://localhost:5173,http://localhost:5174,http://localhost:5175"

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nextgen"
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 5
    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = "CHANGE-ME-IN-PRODUCTION"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    ACCOUNT_LOCKOUT_ATTEMPTS: int = 3
    ACCOUNT_LOCKOUT_MINUTES: int = 60

    @field_validator("ENVIRONMENT")
    @classmethod
    def _check_environment(cls, value: str) -> str:
        return _validate_environment_value(value)


@lru_cache
def get_admin_settings() -> AdminSettings:
    return AdminSettings()