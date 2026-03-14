"""
Database session management — async SQLAlchemy engine + session factory.
Redis connection pool for rate limiting, idempotency, and pubsub.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import get_settings

settings = get_settings()

# ── SQLAlchemy Async Engine ──────────────────────────────────────────────────

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    echo=settings.DEBUG,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
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
        )
    return _redis_pool


async def close_redis():
    """Close Redis pool on shutdown."""
    global _redis_pool
    if _redis_pool:
        await _redis_pool.close()
        _redis_pool = None


# ── Engine lifecycle ─────────────────────────────────────────────────────────

async def close_db():
    """Dispose engine on shutdown."""
    await engine.dispose()
