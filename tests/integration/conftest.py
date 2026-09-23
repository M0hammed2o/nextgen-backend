"""
Integration-test fixtures — a REAL AsyncSession against a real Postgres
instance (the dedicated, disposable docker-compose.test.yml container), not
a mock. This is the first integration-test tier in this codebase's history
(TESTING.md has documented "no integration tests anywhere" as a known gap
since this project's first audit).

Requires TEST_DATABASE_URL (and, for tests that need it, TEST_REDIS_URL) to
point at the dedicated test environment — see docker-compose.test.yml and
TESTING.md's "Integration-testing approach" section for the full workflow:

  docker compose -f docker-compose.test.yml up -d
  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
  export TEST_REDIS_URL="redis://localhost:55678/0"
  python -m alembic upgrade head
  python -m pytest tests/integration -q

This tier is deliberately excluded from the default collection scope
(pytest.ini's `norecursedirs` includes `integration`), so a plain
`pytest tests/ --ignore=tests/replay` does NOT collect or run anything
under this directory — it only runs when explicitly targeted, e.g.
`pytest tests/integration -q`. If TEST_DATABASE_URL/TEST_REDIS_URL aren't
set when this directory IS explicitly targeted, every fixture that needs
them fails loudly (see _env.py) rather than silently connecting to
whichever Postgres/Redis happens to be reachable on localhost.
"""
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration._db import dispose_engine, get_session_factory
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.utils import generate_business_code


@pytest.fixture
async def db() -> AsyncSession:
    async with get_session_factory()() as session:
        yield session
        await session.rollback()  # never leave an open transaction behind
        await session.close()

    # The engine is a module-level singleton (tests/integration/_db.py)
    # whose pool holds asyncpg connections bound to whichever event loop was
    # running when they were created. pytest-asyncio gives every test
    # function its own event loop (asyncio_default_fixture_loop_scope =
    # function, pytest.ini), so a connection checked out by test 2 that was
    # actually opened during test 1's now-closed loop blows up with
    # "RuntimeError: Event loop is closed" the moment SQLAlchemy's
    # pool_pre_ping tries to use it. Disposing the pool after every test
    # forces the next test to open fresh connections on its own loop. This
    # only affects the test-only engine instance's pool bookkeeping, not any
    # production behaviour (production runs one event loop for the process
    # lifetime, so this situation never arises there).
    await dispose_engine()


@pytest.fixture
async def test_business(db: AsyncSession) -> Business:
    """
    A real Business row, created and committed against the real DB, then
    deleted (cascades to every child row via ON DELETE CASCADE) after the
    test — the dev database is left exactly as clean as it was found.
    """
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Integration Test Restaurant {unique}",
        slug=f"integration-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)

    yield business

    # A Core-level DELETE (not session.delete()) so cleanup relies on the
    # DB's own ON DELETE CASCADE on business_users/orders/etc. Business.users
    # is a bidirectional, eagerly-loaded (lazy="selectin") relationship
    # without passive_deletes=True, so session.delete() makes the ORM try to
    # null out business_users.business_id itself before issuing the DELETE —
    # which fails, since that column is NOT NULL. No production code path
    # hard-deletes a Business (businesses are suspended, never deleted), so
    # this is a test-fixture-only workaround, not a model/migration fix.
    #
    # AuditEvent.business_id is deliberately ON DELETE SET NULL, not CASCADE
    # (shared/models/audit.py) — a correct production choice, so an audit
    # trail survives even if its business row is ever removed. But that
    # means deleting the Business alone leaves orphaned, business_id=NULL
    # AuditEvent rows behind forever — a real test-data-pollution bug found
    # by directly querying the DB (123 orphaned rows had silently
    # accumulated across this session's test runs before this fix). Delete
    # them explicitly; this is a test-fixture gap, not a reason to change
    # the production FK's ON DELETE behaviour.
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business.id))
    await db.execute(delete(Business).where(Business.id == business.id))
    await db.commit()
