"""
BUG-0184 follow-up: replacing a credential or deactivating an account must
revoke that user's live refresh tokens, and only that user's.

Covers the paths that previously left sessions refreshable:
- staff PIN rotation (routes_staff.rotate_pin / auto_rotate_pin)
- deactivation through PUT (routes_staff.update_staff, is_active=False)
- deactivation through DELETE (routes_staff.deactivate_staff)
- password change (AuthService.set_password)
"""
from __future__ import annotations

import types
import uuid

import pytest

from backend.app.api.v1.routes_staff import (
    PinRotation,
    StaffUpdate,
    auto_rotate_pin,
    deactivate_staff,
    rotate_pin,
    update_staff,
)
from backend.app.core.rbac import AuthUser
from backend.app.services import auth_service as auth_module
from backend.app.services.auth_service import AuthService


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    """Returns queued results for SELECTs; records every statement."""

    def __init__(self, selects):
        self._selects = list(selects)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed.append(stmt)
        if getattr(stmt, "is_update", False):
            return _Result(None)
        return self._selects.pop(0)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    def revocations(self):
        return [s for s in self.executed if getattr(s, "is_update", False) and s.table.name == "refresh_tokens"]


def _revoked_user_ids(stmt) -> set:
    """The user_id values bound in a refresh-token revocation's WHERE clause."""
    params = stmt.compile().params
    return {v for k, v in params.items() if k.startswith("user_id")}


def _caller(role="OWNER"):
    return AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role=role, token_type="access")


def _staff(business_id, role="STAFF", is_active=True):
    return types.SimpleNamespace(
        token_version=0,  # BusinessUser.token_version server default (migration 0061)
        id=uuid.uuid4(), business_id=business_id, role=role, is_active=is_active,
        staff_name="Thabo", email=None, pin_hash="x", pin_updated_at=None,
        failed_login_attempts=0, locked_until=None,
    )


async def test_rotate_pin_revokes_only_the_target_users_sessions():
    caller = _caller()
    target = _staff(caller.business_id)
    db = _DB([_Result(target)])
    await rotate_pin(staff_id=target.id, body=PinRotation(new_pin="4821"), user=caller, db=db)
    revs = db.revocations()
    assert len(revs) == 1
    assert _revoked_user_ids(revs[0]) == {target.id}
    assert target.token_version == 1  # old access tokens now fail rbac's tv check
    assert db.commits == 1  # PIN change and revocation commit together


async def test_auto_rotate_pin_revokes_only_the_target_users_sessions():
    caller = _caller()
    target = _staff(caller.business_id)
    db = _DB([_Result(target)])
    await auto_rotate_pin(staff_id=target.id, user=caller, db=db)
    revs = db.revocations()
    assert len(revs) == 1 and _revoked_user_ids(revs[0]) == {target.id}
    assert target.token_version == 1


async def test_delete_deactivation_revokes_sessions():
    caller = _caller()
    target = _staff(caller.business_id)
    db = _DB([_Result(target)])
    await deactivate_staff(staff_id=target.id, user=caller, db=db)
    assert target.is_active is False
    revs = db.revocations()
    assert len(revs) == 1 and _revoked_user_ids(revs[0]) == {target.id}


async def test_put_deactivation_revokes_sessions(monkeypatch):
    caller = _caller()
    target = _staff(caller.business_id)
    db = _DB([_Result(target)])
    monkeypatch.setattr("backend.app.api.v1.routes_staff.StaffResponse.model_validate", lambda obj: obj)
    await update_staff(staff_id=target.id, body=StaffUpdate(is_active=False), user=caller, db=db)
    assert target.is_active is False
    revs = db.revocations()
    assert len(revs) == 1 and _revoked_user_ids(revs[0]) == {target.id}


async def test_put_rename_does_not_revoke_sessions(monkeypatch):
    caller = _caller()
    target = _staff(caller.business_id)
    db = _DB([_Result(target)])
    monkeypatch.setattr("backend.app.api.v1.routes_staff.StaffResponse.model_validate", lambda obj: obj)
    await update_staff(staff_id=target.id, body=StaffUpdate(staff_name="Sipho"), user=caller, db=db)
    assert db.revocations() == []
    assert target.token_version == 0  # a rename ends no session


async def test_set_password_revokes_other_sessions_before_issuing_new_tokens(monkeypatch):
    user = types.SimpleNamespace(
        token_version=0,  # BusinessUser.token_version server default (migration 0061)
        id=uuid.uuid4(), business_id=uuid.uuid4(), email="owner@example.com", password_hash="old-hash",
        is_active=True, must_change_password=True, failed_login_attempts=0, locked_until=None, last_login_at=None,
    )
    db = _DB([_Result(user)])
    svc = AuthService(db)
    order = []

    monkeypatch.setattr(auth_module, "verify_password", lambda plain, hashed: True)
    monkeypatch.setattr(auth_module, "hash_password", lambda plain: "new-hash")
    monkeypatch.setattr(svc, "_check_lockout", lambda u: None)
    monkeypatch.setattr(svc, "_check_business_active", lambda b: None)

    async def _load_business(_):
        return types.SimpleNamespace(id=user.business_id, name="Biz")

    async def _issue_tokens(u, ip):
        order.append(("issue", len(db.revocations())))
        return "access", "refresh"

    monkeypatch.setattr(svc, "_load_business", _load_business)
    monkeypatch.setattr(svc, "_issue_tokens", _issue_tokens)

    await svc.set_password(email=user.email, current_password="temp-pw-1", new_password="new-pw-12345")

    revs = db.revocations()
    assert len(revs) == 1 and _revoked_user_ids(revs[0]) == {user.id}
    # Revocation happened before the new pair was issued, so the new session survives.
    assert order == [("issue", 1)]
    assert user.password_hash == "new-hash" and user.must_change_password is False
    assert user.token_version == 1


def test_access_token_carries_tv_and_extra_claims_cannot_override_it():
    import jwt as _jwt
    from backend.app.core.security import create_access_token

    token = create_access_token(uuid.uuid4(), uuid.uuid4(), "STAFF", extra_claims={"tv": 99}, token_version=3)
    assert _jwt.decode(token, options={"verify_signature": False})["tv"] == 3
    default = create_access_token(uuid.uuid4(), uuid.uuid4(), "OWNER")
    assert _jwt.decode(default, options={"verify_signature": False})["tv"] == 0
