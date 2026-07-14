"""
Tests for the PIN length mismatch fix: the staff till's PinKeypad is a fixed
4-digit UI, but PIN schemas allowed 4-8 digits and the auto-rotate endpoint
generated 6 digits — a rotated/created PIN could become impossible to type
on the till. PINs are now standardized to exactly 4 digits everywhere, and
staff creation auto-generates one when omitted (matching the frontend's
existing "Auto-generated if empty" promise).
"""

import types
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.api.v1.routes_staff import (
    PinRotation,
    StaffCreate,
    _generate_pin,
    auto_rotate_pin,
    create_staff,
)
from backend.app.core.rbac import AuthUser


# ── Schema validation ──────────────────────────────────────────────────────────

class TestPinSchemaLength:
    @pytest.mark.parametrize("pin", ["123", "12345", "123456", "12345678"])
    def test_staff_create_rejects_non_4_digit_pin(self, pin):
        with pytest.raises(ValidationError):
            StaffCreate(staff_name="Thabo", pin=pin)

    def test_staff_create_accepts_4_digit_pin(self):
        s = StaffCreate(staff_name="Thabo", pin="1234")
        assert s.pin == "1234"

    @pytest.mark.parametrize("pin", ["123", "123456"])
    def test_pin_rotation_rejects_non_4_digit_pin(self, pin):
        with pytest.raises(ValidationError):
            PinRotation(new_pin=pin)

    def test_pin_rotation_accepts_4_digit_pin(self):
        r = PinRotation(new_pin="4321")
        assert r.new_pin == "4321"


class TestGeneratePin:
    def test_length_is_4(self):
        assert len(_generate_pin()) == 4

    def test_all_digits(self):
        assert _generate_pin().isdigit()

    def test_generates_varied_pins(self):
        pins = {_generate_pin() for _ in range(30)}
        assert len(pins) > 1  # not hardcoded/constant


# ── Fakes ──────────────────────────────────────────────────────────────────────

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

    async def refresh(self, obj):
        # Simulate what a real flush/refresh populates from column defaults.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)


def make_owner_user(business_id):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


# ── create_staff: auto-generate when pin omitted ───────────────────────────────

async def test_create_staff_auto_generates_pin_when_omitted():
    business_id = uuid.uuid4()
    caller = make_owner_user(business_id)
    body = StaffCreate(staff_name="New Staff", role="STAFF")  # no pin

    db = FakeDB([FakeResult(None)])  # email-uniqueness check (skipped, no email)
    result = await create_staff(body=body, user=caller, db=db)

    assert result.initial_pin is not None
    assert len(result.initial_pin) == 4
    assert result.initial_pin.isdigit()


async def test_create_staff_uses_supplied_pin_without_generating():
    business_id = uuid.uuid4()
    caller = make_owner_user(business_id)
    body = StaffCreate(staff_name="New Staff", role="STAFF", pin="9999")

    db = FakeDB([])  # no email supplied -> no uniqueness lookup at all
    result = await create_staff(body=body, user=caller, db=db)

    assert result.initial_pin is None  # only set when auto-generated


# ── auto_rotate_pin: 4 digits ───────────────────────────────────────────────────

async def test_auto_rotate_pin_returns_4_digits():
    business_id = uuid.uuid4()
    caller = make_owner_user(business_id)
    staff = types.SimpleNamespace(
        id=uuid.uuid4(), business_id=business_id,
        pin_hash=None, pin_updated_at=None,
        failed_login_attempts=3, locked_until="something",
    )
    db = FakeDB([FakeResult(staff)])

    res = await auto_rotate_pin(staff_id=staff.id, user=caller, db=db)

    assert len(res.pin) == 4
    assert res.pin.isdigit()
    assert staff.failed_login_attempts == 0
    assert staff.locked_until is None
