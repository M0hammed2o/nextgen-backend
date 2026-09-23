"""
Single shared async engine/session factory for tests/integration/*, built
directly from TEST_DATABASE_URL (see _env.py) — deliberately NOT
`backend.app.db.session.engine`/`async_session_factory`, for two reasons:

1. That module's engine is bound to `settings.DATABASE_URL`, resolved once
   at import time from `get_settings()`. `tests/conftest.py` (loaded before
   any test file, including this directory's own conftest.py) sets a
   DATABASE_URL *default* via `os.environ.setdefault` purely so
   pydantic-settings doesn't need a `.env.backend` file for the (entirely
   mocked) unit suite — but that default value
   (`postgresql+asyncpg://postgres:postgres@localhost/nextgen`, host port
   5432) has actually been observed to route to a live, unrelated Postgres
   instance on a dev machine (see KNOWN_BUGS.md BUG-0096). Using it for a
   REAL connection here would be exactly the ambiguous-target bug this
   module exists to prevent.
2. Whichever module imports `backend.app.db.session` FIRST during test
   collection permanently fixes what that shared engine is bound to for the
   rest of the process — SQLAlchemy engines aren't rebuilt on an env var
   change after the fact. A second, independent footgun.

Every tests/integration/* file that needs a real DB session must use
get_session_factory()/get_engine() from here, never build its own engine
and never import backend.app.db.session's engine/async_session_factory
directly.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration._env import get_test_database_url

_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_test_database_url(),
            pool_pre_ping=True,
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def dispose_engine() -> None:
    """
    Dispose the pool and drop the cached engine/session factory so the next
    fixture rebuilds fresh ones. Needed because pytest-asyncio gives every
    test function its own event loop (asyncio_default_fixture_loop_scope =
    function, pytest.ini): a connection opened during one test's loop blows
    up with "RuntimeError: Event loop is closed" if reused by the next
    test's loop via pool_pre_ping. Call this after every test that touched
    the DB (see tests/integration/conftest.py's `db` fixture).
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
