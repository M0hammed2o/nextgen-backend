# admin_api/app/core/config.py
"""
Admin API settings (control plane).
Uses same DB/Redis/JWT as backend.
Loads env from .env.admin
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.admin",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "NextGen Admin API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/v1"
    ALLOWED_ORIGINS: str = "http://localhost:3002"

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


@lru_cache
def get_admin_settings() -> AdminSettings:
    return AdminSettings()