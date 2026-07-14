"""
Tests for the security hardening work:

Fix #1 — per-staff PIN login: staff pick their name (staff_id), exactly one
         bcrypt hash is verified, and lockout applies to that user only.
Fix #2 — admin/business JWT plane split: tokens from one plane are rejected
         by the other plane's decoder.
Fix #3 — payment credential encryption round-trip and legacy passthrough.

The suite runs without a live database (see conftest.py), so AuthService is
exercised against a fake AsyncSession.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from backend.app.core.errors import AppError
from backend.app.core.security import (
    create_access_token,
    create_admin_access_token,
    decode_access_token,
    decode_admin_token,
    hash_pin,
)
from backend.app.services.auth_service import AuthService


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, value=None, many=None):
        self._value = value
        self._many = many if many is not None else []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        m = MagicMock()
        m.all.return_value = self._many
        return m


class FakeDB:
    """Minimal AsyncSession stand-in: returns queued results in order."""

    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(),
        name="Test Cafe",
        business_code="ABC123",
        is_active=True,
        suspended_reason=None,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def make_staff(pin="1234", **overrides):
    s = types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=None,
        role="STAFF",
        staff_name="Thabo",
        email=None,
        pin_hash=hash_pin(pin),
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        last_login_at=None,
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ── Fix #1: per-staff PIN login ──────────────────────────────────────────────

async def test_login_pin_success_resets_lockout_counter():
    business = make_business()
    staff = make_staff(pin="4321", business_id=business.id, failed_login_attempts=3)
    db = FakeDB([FakeResult(business), FakeResult(staff)])

    user, biz, access, refresh = await AuthService(db).login_pin(
        business_code="abc123", staff_id=staff.id, pin="4321"
    )

    assert user is staff
    assert biz is business
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
    assert db.commits == 1
    payload = decode_access_token(access)
    assert payload["role"] == "STAFF"
    assert payload["bid"] == str(business.id)
    # STAFF access tokens are short-lived (10 min default)
    assert payload["exp"] - payload["iat"] == 10 * 60


async def test_login_pin_wrong_pin_penalizes_only_selected_staff():
    """The DoS regression test: a wrong PIN must never touch sibling staff."""
    business = make_business()
    target = make_staff(pin="1111", business_id=business.id)
    sibling = make_staff(pin="2222", business_id=business.id, staff_name="Lerato")

    db = FakeDB([FakeResult(business), FakeResult(target)])
    with pytest.raises(AppError) as exc:
        await AuthService(db).login_pin(
            business_code="ABC123", staff_id=target.id, pin="9999"
        )

    assert exc.value.status_code == 401
    assert target.failed_login_attempts == 1
    assert sibling.failed_login_attempts == 0  # untouched — never even loaded
    assert db.commits == 1  # failed attempt is persisted (get_db never commits)


async def test_login_pin_lockout_after_max_attempts_only_for_that_user():
    business = make_business()
    staff = make_staff(pin="1111", business_id=business.id)

    for _ in range(5):
        db = FakeDB([FakeResult(business), FakeResult(staff)])
        with pytest.raises(AppError):
            await AuthService(db).login_pin(
                business_code="ABC123", staff_id=staff.id, pin="0000"
            )

    assert staff.failed_login_attempts == 5
    assert staff.locked_until is not None
    assert staff.locked_until > datetime.now(timezone.utc)

    # 6th attempt → 423 locked, even with the RIGHT pin
    db = FakeDB([FakeResult(business), FakeResult(staff)])
    with pytest.raises(AppError) as exc:
        await AuthService(db).login_pin(
            business_code="ABC123", staff_id=staff.id, pin="1111"
        )
    assert exc.value.status_code == 423


async def test_login_pin_unknown_business_is_generic_401():
    db = FakeDB([FakeResult(None)])
    with pytest.raises(AppError) as exc:
        await AuthService(db).login_pin(
            business_code="ZZZZZZ", staff_id=uuid.uuid4(), pin="1234"
        )
    assert exc.value.status_code == 401
    assert exc.value.code == "INVALID_CREDENTIALS"


async def test_login_pin_unknown_or_foreign_staff_id_is_generic_401():
    business = make_business()
    db = FakeDB([FakeResult(business), FakeResult(None)])
    with pytest.raises(AppError) as exc:
        await AuthService(db).login_pin(
            business_code="ABC123", staff_id=uuid.uuid4(), pin="1234"
        )
    assert exc.value.status_code == 401


async def test_staff_directory_returns_business_and_staff():
    business = make_business()
    staff = [make_staff(business_id=business.id), make_staff(business_id=business.id, staff_name="Lerato")]
    db = FakeDB([FakeResult(business), FakeResult(many=staff)])

    biz, entries = await AuthService(db).get_staff_directory("abc123")
    assert biz is business
    assert entries == staff


async def test_staff_directory_unknown_code_is_generic_401():
    db = FakeDB([FakeResult(None)])
    with pytest.raises(AppError) as exc:
        await AuthService(db).get_staff_directory("ZZZZZZ")
    assert exc.value.status_code == 401
    assert exc.value.code == "INVALID_CREDENTIALS"


# ── Fix #2: JWT plane split ──────────────────────────────────────────────────

def test_business_token_rejected_by_admin_decoder():
    token = create_access_token(uuid.uuid4(), uuid.uuid4(), "OWNER")
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_admin_token(token)


def test_admin_token_rejected_by_business_decoder():
    token = create_admin_access_token(uuid.uuid4())
    with pytest.raises(pyjwt.InvalidTokenError):
        decode_access_token(token)


def test_each_plane_round_trips_its_own_tokens():
    biz_token = create_access_token(uuid.uuid4(), uuid.uuid4(), "MANAGER")
    assert decode_access_token(biz_token)["type"] == "access"

    admin_token = create_admin_access_token(uuid.uuid4())
    payload = decode_admin_token(admin_token)
    assert payload["type"] == "admin_access"
    assert payload["role"] == "SUPER_ADMIN"


# ── Fix #3: credential encryption ────────────────────────────────────────────

def test_credentials_passthrough_without_key():
    """No CREDENTIALS_ENCRYPTION_KEY configured (dev) — values stored as-is."""
    from backend.app.core import crypto

    crypto._fernet.cache_clear()
    assert crypto.encrypt_credential("sk_live_abc") == "sk_live_abc"
    assert crypto.decrypt_credential("sk_live_abc") == "sk_live_abc"
    assert crypto.encrypt_credential(None) is None
    assert crypto.decrypt_credential(None) is None


def test_credentials_encrypt_decrypt_round_trip():
    from cryptography.fernet import Fernet

    from backend.app.core import crypto
    from backend.app.core.config import get_settings

    settings = get_settings()
    original = settings.CREDENTIALS_ENCRYPTION_KEY
    settings.CREDENTIALS_ENCRYPTION_KEY = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    try:
        stored = crypto.encrypt_credential("sk_live_abc123")
        assert stored.startswith("enc1:")
        assert "sk_live" not in stored
        assert crypto.decrypt_credential(stored) == "sk_live_abc123"
        # Legacy plaintext rows still pass through even with a key configured
        assert crypto.decrypt_credential("legacy-plain-key") == "legacy-plain-key"
    finally:
        settings.CREDENTIALS_ENCRYPTION_KEY = original
        crypto._fernet.cache_clear()
