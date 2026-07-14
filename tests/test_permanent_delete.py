"""
Tests for permanent staff deletion — a genuine hard-delete alongside the
existing soft-deactivate. OWNER only, and only once the target is already
inactive (two-step gate: deactivate first, purge later).
"""

import types
import uuid

import pytest

from backend.app.api.v1.routes_staff import delete_staff_permanently
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.commits = 0
        self.deleted = []

    async def execute(self, stmt):
        return self._results.pop(0)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1


def make_caller(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_staff(**overrides):
    s = types.SimpleNamespace(id=uuid.uuid4(), business_id=uuid.uuid4(), is_active=False)
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


async def test_owner_can_permanently_delete_inactive_staff():
    caller = make_caller(role="OWNER")
    staff = make_staff(business_id=caller.business_id, is_active=False)
    db = FakeDB([FakeResult(staff)])

    await delete_staff_permanently(staff_id=staff.id, user=caller, db=db)

    assert staff in db.deleted
    assert db.commits == 1


async def test_manager_cannot_permanently_delete():
    caller = make_caller(role="MANAGER")
    staff = make_staff(business_id=caller.business_id, is_active=False)
    db = FakeDB([])  # role check happens before any DB lookup

    with pytest.raises(AppError) as exc:
        await delete_staff_permanently(staff_id=staff.id, user=caller, db=db)

    assert exc.value.code == "INSUFFICIENT_ROLE"
    assert exc.value.status_code == 403
    assert db.deleted == []


async def test_cannot_permanently_delete_active_staff():
    caller = make_caller(role="OWNER")
    staff = make_staff(business_id=caller.business_id, is_active=True)
    db = FakeDB([FakeResult(staff)])

    with pytest.raises(AppError) as exc:
        await delete_staff_permanently(staff_id=staff.id, user=caller, db=db)

    assert exc.value.code == "STILL_ACTIVE"
    assert exc.value.status_code == 422
    assert db.deleted == []


async def test_cannot_permanently_delete_self():
    caller = make_caller(role="OWNER")
    staff = make_staff(id=caller.user_id, business_id=caller.business_id, is_active=False)
    db = FakeDB([FakeResult(staff)])

    with pytest.raises(AppError) as exc:
        await delete_staff_permanently(staff_id=staff.id, user=caller, db=db)

    assert exc.value.code == "SELF_DELETION"
    assert db.deleted == []


async def test_unknown_staff_is_404():
    caller = make_caller(role="OWNER")
    db = FakeDB([FakeResult(None)])

    with pytest.raises(NotFoundError) as exc:
        await delete_staff_permanently(staff_id=uuid.uuid4(), user=caller, db=db)

    assert exc.value.status_code == 404
