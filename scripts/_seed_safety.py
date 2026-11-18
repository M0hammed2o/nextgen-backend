"""
Shared safety rails for the seed/QA scripts in this directory (BUG-0184).

Two rules:

1. A seed script must never write to a database that does not look local or
   disposable unless the operator explicitly passes ``--yes``. The fixture
   accounts these scripts create have well-known passwords; if they ever
   landed in production, anyone reading this public repository could log in.
2. Fixture credentials can be overridden from the environment. The built-in
   defaults are kept only because the local Playwright suite (``e2e-tests/``)
   logs in with them against a throwaway database.

Live-tenant credentials never belong in this repository at all. Scripts that
seed a real tenant (``seed_mochachos_demo.py``) take them from the
environment or generate random ones, and have no default.

Nothing here reads, logs or prints a connection string or a credential.
"""
from __future__ import annotations

import os
import sys

# Exact hostnames only. Loopback addresses plus the docker-compose service
# names used by this repo's local stacks. Never a substring match: a remote
# database *named* nextgen_test, a password containing "localhost", or a host
# like localhost.evil.example.com must all count as remote.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})


def looks_local(database_url: str | None) -> bool:
    """True only when the URL's parsed host is exactly a known local host. Fails closed."""
    if not database_url:
        return False
    try:
        from sqlalchemy.engine import make_url

        host = make_url(database_url).host
    except Exception:
        return False
    return (host or "").strip("[]").lower() in _LOCAL_HOSTS


def require_local_or_confirmed(database_url: str, script_name: str, argv: list[str] | None = None) -> None:
    """Exit with status 2 unless the target looks local or ``--yes`` was passed."""
    args = sys.argv[1:] if argv is None else argv
    if looks_local(database_url) or "--yes" in args:
        return
    print(
        f"{script_name}: refusing to write. DATABASE_URL does not look like a local or disposable "
        "database. This script creates fixture accounts with publicly known passwords. "
        "Re-run with --yes only if you really mean to seed this target, and set the credential "
        "environment variables first. (The connection string is not printed.)",
        file=sys.stderr,
    )
    raise SystemExit(2)


def fixture_credential(env_var: str, default: str) -> str:
    """Environment override for a fixture credential; the default is for disposable databases only."""
    return os.environ.get(env_var) or default
