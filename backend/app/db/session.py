"""
Database session management — async SQLAlchemy engine + session factory.
Redis connection pool for rate limiting, idempotency, and pubsub.

Updated for:
- Supabase Session Pooler
- Render deployment
- safer async connection handling
- better pool stability
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import get_settings

settings = get_settings()


# ── DATABASE URL NORMALIZATION ───────────────────────────────────────────────

def _normalize_database_url(url: str) -> str:
    """
    Ensure SQLAlchemy is using the asyncpg driver.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _normalize_database_url(settings.DATABASE_URL)


# ── SQLAlchemy Async Engine ──────────────────────────────────────────────────
# Notes:
# - pool_pre_ping=True helps recover stale connections on Render/Supabase
# - pool_recycle avoids old broken connections hanging around too long
# - statement_cache_size=0 is safer with pooled PostgreSQL environments
# - command_timeout prevents hanging forever on bad queries

engine = create_async_engine(
    DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=settings.DEBUG,
    future=True,
    connect_args={
        "statement_cache_size": 0,
        "command_timeout": 30,
        "server_settings": {
            "application_name": "nextgen-backend",
        },
    },
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session and handles cleanup."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


# ── Redis ────────────────────────────────────────────────────────────────────

_redis_pool = None


async def get_redis():
    """Get the shared Redis connection pool. Lazy-initialized."""
    global _redis_pool
    if _redis_pool is None:
        import redis.asyncio as aioredis

        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _redis_pool


async def close_redis():
    """Close Redis pool on shutdown."""
    global _redis_pool
    if _redis_pool is not None:
        try:
            await _redis_pool.aclose()
        except AttributeError:
            await _redis_pool.close()
        _redis_pool = None


# ── Engine lifecycle ─────────────────────────────────────────────────────────

async def close_db():
    """Dispose engine on shutdown."""
    await engine.dispose()
