"""
BUG-0184: seed scripts must not carry live-tenant credentials, and must refuse
to write fixture accounts with publicly known passwords to a non-local
database unless the operator passes --yes.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts._seed_safety import (  # noqa: E402
    fixture_credential,
    looks_local,
    require_local_or_confirmed,
)

LOCAL = "postgresql+asyncpg://u:p@localhost:5432/db"
REMOTE = "postgresql+asyncpg://u:p@db.example-pooler.supabase.com:6543/postgres"

GUARDED_SCRIPTS = [
    "scripts/seed.py",
    "scripts/seed_whatsapp_only_qa.py",
    "scripts/seed_split_payments_e2e.py",
    "scripts/seed_split_payments_e2e_tenant_b.py",
]


@pytest.mark.parametrize(
    "url",
    [
        LOCAL,
        "postgresql://u:p@127.0.0.1/db",
        "postgresql+asyncpg://nextgen_test:p@localhost:55677/nextgen_test",
        "postgresql://u:p@postgres:5432/db",
        "postgresql://u:p@[::1]:5432/db",
    ],
)
def test_local_urls_are_recognised(url):
    assert looks_local(url)


@pytest.mark.parametrize(
    "url",
    [
        REMOTE,
        "postgresql://u:p@api.nextgenintelligence.co.za/db",
        "",
        None,
        "not a url",
        # Security review 2026-11-18: each of these fooled the old substring check.
        "postgresql://u:p@dpg-abc123.oregon-postgres.render.com/nextgen_test",
        "postgresql://nextgen_test_user:p@db.abcdef.supabase.co:5432/postgres",
        "postgresql://u:has-localhost-in-it@db.abcdef.supabase.co:5432/postgres",
        "postgresql://u:p@db.abcdef.supabase.co:5432/postgres?application_name=from-localhost",
        "postgresql://u:p@localhost.evil.example.com/db",
        "postgresql://u:p@[2001:db8::1]:5432/db",
        "postgresql://u:p@127.0.0.10.nip.io/db",
        "postgresql://postgres.abcdef:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
    ],
)
def test_remote_or_empty_urls_are_not_local(url):
    assert not looks_local(url)


def test_remote_target_without_yes_exits_2_and_does_not_print_the_url(capsys):
    with pytest.raises(SystemExit) as exc:
        require_local_or_confirmed(REMOTE, "scripts/seed.py", argv=[])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "refusing to write" in err
    assert "supabase" not in err and "u:p@" not in err


def test_remote_target_with_yes_is_allowed():
    require_local_or_confirmed(REMOTE, "scripts/seed.py", argv=["--yes"])


def test_local_target_needs_no_flag():
    require_local_or_confirmed(LOCAL, "scripts/seed.py", argv=[])


def test_fixture_credential_env_override(monkeypatch):
    monkeypatch.setenv("NEXTGEN_TEST_OVERRIDE", "from-env")
    assert fixture_credential("NEXTGEN_TEST_OVERRIDE", "default") == "from-env"
    monkeypatch.delenv("NEXTGEN_TEST_OVERRIDE")
    assert fixture_credential("NEXTGEN_TEST_OVERRIDE", "default") == "default"


async def test_seed_py_refuses_remote_database_before_opening_a_connection(monkeypatch):
    """Behavioural: the guard runs before create_async_engine is ever reached."""
    import scripts.seed as seed_module

    monkeypatch.setattr(sys, "argv", ["seed.py"])
    fake_settings = SimpleNamespace(DATABASE_URL=REMOTE)
    with patch("backend.app.core.config.get_settings", return_value=fake_settings), patch(
        "sqlalchemy.ext.asyncio.create_async_engine",
        side_effect=AssertionError("engine must not be created for a remote target"),
    ):
        with pytest.raises(SystemExit) as exc:
            await seed_module.seed()
    assert exc.value.code == 2


@pytest.mark.parametrize("rel", GUARDED_SCRIPTS)
def test_every_fixture_seed_guards_before_creating_its_engine(rel):
    """Static: the guard call appears, and appears before the engine is created."""
    src = (REPO / rel).read_text(encoding="utf-8")
    guard = src.find("require_local_or_confirmed(settings.DATABASE_URL")
    engine = src.find("create_async_engine(settings.DATABASE_URL)")
    assert guard != -1, f"{rel} has no local-target guard"
    assert engine != -1 and guard < engine, f"{rel} creates its engine before the guard"


@pytest.mark.parametrize(
    "rel",
    [
        "scripts/verify_pilot_till_stocktake_e2e.py",
        "scripts/verify_ciao_e2e.py",
        "scripts/verify_external_orders_e2e.py",
        "scripts/verify_self_service_e2e.py",
    ],
)
def test_verify_scripts_guard_before_creating_their_engine(rel):
    src = (REPO / rel).read_text(encoding="utf-8")
    guard = src.find('require_local_or_confirmed(os.environ["DATABASE_URL"]')
    engine = src.find('create_async_engine(os.environ["DATABASE_URL"]')
    assert guard != -1 and engine != -1 and guard < engine


def _string_assignments(rel: str) -> dict[str, str]:
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def test_live_tenant_seed_has_no_credential_literals():
    """seed_mochachos_demo.py seeds a real production tenant: no password/PIN may be a literal."""
    assigned = _string_assignments("scripts/seed_mochachos_demo.py")
    offenders = [n for n in assigned if any(k in n.upper() for k in ("PASSWORD", "PIN", "SECRET", "TOKEN"))]
    assert offenders == [], f"credential-like literals in the live-tenant seed: {offenders}"


@pytest.mark.parametrize("rel", GUARDED_SCRIPTS)
def test_fixture_credentials_are_overridable(rel):
    """No fixture password/PIN constant in the guarded scripts is a bare literal any more."""
    assigned = _string_assignments(rel)
    bare = [n for n in assigned if n.isupper() and ("PASSWORD" in n or n.endswith("_PIN"))]
    assert bare == [], f"{rel}: bare credential literals {bare}"
