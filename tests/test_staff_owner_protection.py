"""
Staff-management role-hierarchy guard (Wave 0A P1 security fix).

Before this fix, update_staff/rotate_pin/auto_rotate_pin/deactivate_staff
loaded the target BusinessUser by id + business only, with NO check on the
target's role — a MANAGER could deactivate, re-PIN, or (via PUT is_active)
silently reactivate/deactivate the OWNER, who has no self-service recovery.

Rule now enforced by `_guard_target` (and the equivalent inline checks in
deactivate_staff/delete_staff_permanently, which keep their pre-existing
SELF_DEACTIVATION/SELF_DELETION codes for the self case):
  - a MANAGER may only act on STAFF targets
  - an OWNER may act on STAFF and MANAGER targets
  - nobody may act on themself or on any OWNER through these routes
  - cross-role attempts are 403; unknown/other-tenant ids stay 404
"""
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
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_user(role: str, business_id=None, user_id=None) -> AuthUser:
    return AuthUser(
        user_id=user_id or uuid.uuid4(),
        business_id=business_id or uuid.uuid4(),
        role=role,
        token_type="access",
    )


def make_staff(role: str, business_id, staff_id=None, is_active=True):
    from datetime import datetime, timezone

    return types.SimpleNamespace(
        id=staff_id or uuid.uuid4(),
        business_id=business_id,
        role=role,
        staff_name="Target",
        email=None,
        is_active=is_active,
        pin_hash=None,
        pin_updated_at=None,
        failed_login_attempts=0,
        locked_until=None,
        last_login_at=None,
        created_at=datetime.now(timezone.utc),
    )


# ── update_staff ─────────────────────────────────────────────────────────────

async def test_manager_can_update_staff():
    manager = make_user("MANAGER")
    staff = make_staff("STAFF", manager.business_id)
    db = FakeDB([FakeResult(staff)])

    result = await update_staff(
        staff_id=staff.id, body=StaffUpdate(staff_name="New Name"), user=manager, db=db,
    )
    assert result.staff_name == "New Name"
    assert db.commits == 1


async def test_manager_cannot_update_another_manager():
    manager = make_user("MANAGER")
    target = make_staff("MANAGER", manager.business_id)
    db = FakeDB([FakeResult(target)])

    with pytest.raises(AppError) as exc:
        await update_staff(staff_id=target.id, body=StaffUpdate(staff_name="X"), user=manager, db=db)
    assert exc.value.code == "INSUFFICIENT_ROLE"
    assert exc.value.status_code == 403
    assert db.commits == 0


async def test_owner_can_update_manager():
    owner = make_user("OWNER")
    target = make_staff("MANAGER", owner.business_id)
    db = FakeDB([FakeResult(target)])

    result = await update_staff(staff_id=target.id, body=StaffUpdate(staff_name="Y"), user=owner, db=db)
    assert result.staff_name == "Y"


async def test_nobody_can_update_the_owner():
    manager = make_user("MANAGER")
    owner_target = make_staff("OWNER", manager.business_id)
    db = FakeDB([FakeResult(owner_target)])

    with pytest.raises(AppError) as exc:
        await update_staff(staff_id=owner_target.id, body=StaffUpdate(staff_name="Z"), user=manager, db=db)
    assert exc.value.code == "CANNOT_TARGET_OWNER"
    assert exc.value.status_code == 403


async def test_owner_cannot_update_themself_via_staff_route():
    owner = make_user("OWNER")
    self_target = make_staff("OWNER", owner.business_id, staff_id=owner.user_id)
    db = FakeDB([FakeResult(self_target)])

    with pytest.raises(AppError) as exc:
        await update_staff(staff_id=owner.user_id, body=StaffUpdate(is_active=False), user=owner, db=db)
    assert exc.value.code == "SELF_ACTION_FORBIDDEN"
    assert exc.value.status_code == 403


async def test_update_staff_unknown_id_is_404():
    manager = make_user("MANAGER")
    db = FakeDB([FakeResult(None)])
    with pytest.raises(NotFoundError):
        await update_staff(staff_id=uuid.uuid4(), body=StaffUpdate(staff_name="X"), user=manager, db=db)


# ── rotate_pin / auto_rotate_pin ─────────────────────────────────────────────

async def test_manager_cannot_rotate_pin_of_another_manager():
    manager = make_user("MANAGER")
    target = make_staff("MANAGER", manager.business_id)
    db = FakeDB([FakeResult(target)])

    with pytest.raises(AppError) as exc:
        await rotate_pin(staff_id=target.id, body=PinRotation(new_pin="1234"), user=manager, db=db)
    assert exc.value.code == "INSUFFICIENT_ROLE"
    assert db.commits == 0


async def test_manager_can_rotate_pin_of_staff():
    manager = make_user("MANAGER")
    target = make_staff("STAFF", manager.business_id)
    db = FakeDB([FakeResult(target)])

    result = await rotate_pin(staff_id=target.id, body=PinRotation(new_pin="1234"), user=manager, db=db)
    assert result.pin == "1234"


async def test_auto_rotate_pin_blocks_targeting_owner():
    manager = make_user("MANAGER")
    owner_target = make_staff("OWNER", manager.business_id)
    db = FakeDB([FakeResult(owner_target)])

    with pytest.raises(AppError) as exc:
        await auto_rotate_pin(staff_id=owner_target.id, user=manager, db=db)
    assert exc.value.code == "CANNOT_TARGET_OWNER"


# ── deactivate_staff ─────────────────────────────────────────────────────────

async def test_manager_cannot_deactivate_another_manager():
    manager = make_user("MANAGER")
    target = make_staff("MANAGER", manager.business_id)
    db = FakeDB([FakeResult(target)])

    with pytest.raises(AppError) as exc:
        await deactivate_staff(staff_id=target.id, user=manager, db=db)
    assert exc.value.code == "INSUFFICIENT_ROLE"
    assert exc.value.status_code == 403
    assert db.commits == 0


async def test_manager_can_deactivate_staff():
    manager = make_user("MANAGER")
    target = make_staff("STAFF", manager.business_id)
    db = FakeDB([FakeResult(target), FakeResult(None)])

    await deactivate_staff(staff_id=target.id, user=manager, db=db)
    assert target.is_active is False
    assert db.commits == 1


async def test_nobody_can_deactivate_the_owner():
    owner = make_user("OWNER")
    other_owner = make_staff("OWNER", owner.business_id)  # e.g. legacy multi-owner row
    db = FakeDB([FakeResult(other_owner)])

    with pytest.raises(AppError) as exc:
        await deactivate_staff(staff_id=other_owner.id, user=owner, db=db)
    assert exc.value.code == "CANNOT_TARGET_OWNER"


async def test_self_deactivation_keeps_existing_code():
    """Pre-existing behaviour/error code must survive this change unmodified."""
    manager = make_user("MANAGER")
    self_target = make_staff("MANAGER", manager.business_id, staff_id=manager.user_id)
    db = FakeDB([FakeResult(self_target)])

    with pytest.raises(AppError) as exc:
        await deactivate_staff(staff_id=manager.user_id, user=manager, db=db)
    assert exc.value.code == "SELF_DEACTIVATION"
    assert exc.value.status_code == 422
