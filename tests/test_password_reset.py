"""
Tests for the temp-password + forced-reset flow (OWNER/MANAGER accounts):

- generate_temp_password(): length and unambiguous-character alphabet.
- login_email(): must_change_password=True blocks normal login with
  PASSWORD_CHANGE_REQUIRED and issues no tokens; must_change_password=False
  still logs in normally (regression check on the new gate).
- set_password(): wrong current_password fails and penalizes lockout; same
  new/current password is rejected; the correct flow updates the hash,
  clears the flag, and returns tokens (auto-login).
"""

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from backend.app.core.errors import AppError
from backend.app.core.security import hash_password, verify_password
from backend.app.services.auth_service import AuthService
from shared.utils import generate_temp_password


# ── generate_temp_password ────────────────────────────────────────────────────

class TestGenerateTempPassword:
    def test_default_length(self):
        assert len(generate_temp_password()) == 10

    def test_custom_length(self):
        assert len(generate_temp_password(length=16)) == 16

    def test_excludes_ambiguous_characters(self):
        ambiguous = set("0O1lI")
        for _ in range(50):
            pw = generate_temp_password()
            assert not (set(pw) & ambiguous), f"{pw!r} contains an ambiguous character"

    def test_generates_distinct_passwords(self):
        passwords = {generate_temp_password() for _ in range(50)}
        assert len(passwords) == 50  # vanishingly unlikely to collide


# ── Fakes (same lightweight pattern as tests/test_auth_pin.py) ────────────────

class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        pass

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


def make_business(**overrides):
    b = types.SimpleNamespace(id=uuid.uuid4(), name="Test Cafe", is_active=True, suspended_reason=None)
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def make_owner(password="Temp1234", **overrides):
    u = types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        role="OWNER",
        email="owner@example.com",
        staff_name="Jane Owner",
        password_hash=hash_password(password),
        is_active=True,
        must_change_password=False,
        failed_login_attempts=0,
        locked_until=None,
        last_login_at=None,
    )
    for k, v in overrides.items():
        setattr(u, k, v)
    return u


# ── login_email: must_change_password gate ───────────────────────────────────

async def test_login_email_blocks_when_must_change_password():
    owner = make_owner(password="Temp1234", must_change_password=True)
    business = make_business(id=owner.business_id)
    db = FakeDB([FakeResult(owner), FakeResult(business)])

    with pytest.raises(AppError) as exc:
        await AuthService(db).login_email(email=owner.email, password="Temp1234")

    assert exc.value.code == "PASSWORD_CHANGE_REQUIRED"
    assert exc.value.status_code == 403
    assert db.commits == 1  # lockout/last_login bookkeeping still persisted


async def test_login_email_succeeds_when_password_change_not_required():
    owner = make_owner(password="Temp1234", must_change_password=False)
    business = make_business(id=owner.business_id)
    db = FakeDB([FakeResult(owner), FakeResult(business)])

    user, biz, access, refresh = await AuthService(db).login_email(
        email=owner.email, password="Temp1234"
    )
    assert user is owner
    assert biz is business
    assert access and refresh


# ── set_password ──────────────────────────────────────────────────────────────

async def test_set_password_wrong_current_password_fails_and_penalizes():
    owner = make_owner(password="Temp1234", must_change_password=True)
    db = FakeDB([FakeResult(owner)])

    with pytest.raises(AppError) as exc:
        await AuthService(db).set_password(
            email=owner.email, current_password="WrongOne", new_password="BrandNew1"
        )

    assert exc.value.code == "INVALID_CREDENTIALS"
    assert exc.value.status_code == 401
    assert owner.failed_login_attempts == 1
    assert db.commits == 1


async def test_set_password_rejects_same_password():
    owner = make_owner(password="Temp1234", must_change_password=True)
    db = FakeDB([FakeResult(owner)])

    with pytest.raises(AppError) as exc:
        await AuthService(db).set_password(
            email=owner.email, current_password="Temp1234", new_password="Temp1234"
        )

    assert exc.value.code == "SAME_PASSWORD"
    assert exc.value.status_code == 422


async def test_set_password_success_updates_hash_and_clears_flag():
    owner = make_owner(password="Temp1234", must_change_password=True)
    business = make_business(id=owner.business_id)
    db = FakeDB([FakeResult(owner), FakeResult(business)])

    user, biz, access, refresh = await AuthService(db).set_password(
        email=owner.email, current_password="Temp1234", new_password="BrandNew1"
    )

    assert user.must_change_password is False
    assert verify_password("BrandNew1", user.password_hash)
    assert not verify_password("Temp1234", user.password_hash)
    assert access and refresh
    assert db.commits == 1


async def test_set_password_unknown_email_is_generic_401():
    db = FakeDB([FakeResult(None)])

    with pytest.raises(AppError) as exc:
        await AuthService(db).set_password(
            email="nobody@example.com", current_password="x", new_password="BrandNew1"
        )

    assert exc.value.code == "INVALID_CREDENTIALS"
    assert exc.value.status_code == 401
