"""
Tests for the admin dashboard's new owner/manager password-reset flow:
listing existing owner/manager logins for a business (previously impossible
— the admin dashboard could only create new ones), and resetting an
existing account's password (generates a new temp password, forces the
same must_change_password reset flow used at creation, and revokes live
sessions — same pattern as staff deactivation).
"""

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from admin_api.app.api.v1.routes_businesses import (
    list_business_users,
    reset_business_user_password,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import verify_password


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
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def get(self, model, id_):
        return self._results.pop(0)

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_admin_user():
    return AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


def make_business(**overrides):
    b = types.SimpleNamespace(id=uuid.uuid4())
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def make_business_user(password="OldTemp123", **overrides):
    from backend.app.core.security import hash_password
    u = types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=uuid.uuid4(),
        role="OWNER",
        email="owner@example.com",
        staff_name="Jane Owner",
        password_hash=hash_password(password),
        is_active=True,
        must_change_password=False,
        failed_login_attempts=2,
        locked_until="something",
        last_login_at=None,
        created_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(u, k, v)
    return u


# ── list_business_users ────────────────────────────────────────────────────────

async def test_list_business_users_returns_owners_and_managers():
    biz = make_business()
    owner = make_business_user(role="OWNER", business_id=biz.id)
    manager = make_business_user(role="MANAGER", business_id=biz.id, email="mgr@example.com")

    db = FakeDB([biz, FakeResult(many=[owner, manager])])
    result = await list_business_users(business_id=biz.id, user=make_admin_user(), db=db)

    assert len(result) == 2
    assert {r.role for r in result} == {"OWNER", "MANAGER"}


async def test_list_business_users_404_for_unknown_business():
    db = FakeDB([None])
    with pytest.raises(NotFoundError) as exc:
        await list_business_users(business_id=uuid.uuid4(), user=make_admin_user(), db=db)
    assert exc.value.status_code == 404


# ── reset_business_user_password ───────────────────────────────────────────────

async def test_reset_password_generates_new_temp_password_and_flags_reset():
    biz = make_business()
    target = make_business_user(password="OldTemp123", business_id=biz.id, must_change_password=False)

    db = FakeDB([FakeResult(target), FakeResult(None)])  # select target, then the revoke-tokens update
    res = await reset_business_user_password(
        business_id=biz.id, user_id=target.id, user=make_admin_user(), db=db
    )

    assert res.temporary_password
    assert target.must_change_password is True
    assert target.failed_login_attempts == 0
    assert target.locked_until is None
    # New password actually replaces the old hash
    assert verify_password(res.temporary_password, target.password_hash)
    assert not verify_password("OldTemp123", target.password_hash)
    assert db.commits == 1


async def test_reset_password_404_for_unknown_user():
    db = FakeDB([FakeResult(None)])
    with pytest.raises(AppError) as exc:
        await reset_business_user_password(
            business_id=uuid.uuid4(), user_id=uuid.uuid4(), user=make_admin_user(), db=db
        )
    assert exc.value.status_code == 404
