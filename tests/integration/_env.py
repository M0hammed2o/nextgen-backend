"""
Single source of truth for how every tests/integration/* file resolves the
dedicated test Postgres/Redis instances. See docker-compose.test.yml for the
disposable environment this is meant to point at.

Deliberately fail-closed (KNOWN_BUGS.md BUG-0096): this module has NO
ambient "localhost" default of any kind, and it does not reuse the app's own
`DATABASE_URL`/`REDIS_URL` settings. A dev machine can have several
unrelated live Postgres/Redis instances listening on common ports at once
(a nextgen-staging stack, Supabase, other unrelated projects' containers) —
silently guessing one of them, or silently falling back to whatever
`backend.app.core.config.Settings.DATABASE_URL` happens to default to, is
exactly the defect this module exists to prevent. If TEST_DATABASE_URL /
TEST_REDIS_URL aren't set, every fixture that needs them raises a loud,
actionable error instead of connecting to "whatever is on localhost".

Every tests/integration/* file must call get_test_database_url() /
get_test_redis_url() (directly or via tests/integration/_db.py) rather than
hardcoding its own default connection string. See TESTING.md's
"Integration-testing approach" section for the full documented workflow.
"""
from __future__ import annotations

import os

_SETUP_INSTRUCTIONS = """
Integration tests (tests/integration/*) require a dedicated, positively
identified test database and Redis instance. This repo deliberately never
guesses a "localhost default" for this tier — a dev machine can have
several unrelated live Postgres instances listening on common ports at
once, and silently connecting to one of them is not acceptable for a test
suite (see KNOWN_BUGS.md BUG-0096).

To run this tier:

    docker compose -f docker-compose.test.yml up -d
    export TEST_DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test"
    export TEST_REDIS_URL="redis://localhost:55678/0"
    python -m alembic upgrade head
    python -m pytest tests/integration -q

Then tear down (destroys the container and its data volume):

    docker compose -f docker-compose.test.yml down -v

See TESTING.md for the full documented workflow.
""".strip("\n")


class DedicatedTestEnvironmentNotConfigured(RuntimeError):
    """
    Raised when tests/integration/* is invoked without TEST_DATABASE_URL /
    TEST_REDIS_URL set. Deliberately a plain RuntimeError, and deliberately
    NOT caught anywhere as a reason to `pytest.skip` — this tier must fail
    LOUDLY (a visible ERROR naming the exact fix) when run without following
    the documented setup, never report a quietly-green "skipped" result that
    could be mistaken for "ran clean".
    """


def get_test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise DedicatedTestEnvironmentNotConfigured(
            "TEST_DATABASE_URL is not set.\n\n" + _SETUP_INSTRUCTIONS
        )
    return url


def get_test_redis_url() -> str:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        raise DedicatedTestEnvironmentNotConfigured(
            "TEST_REDIS_URL is not set.\n\n" + _SETUP_INSTRUCTIONS
        )
    return url
