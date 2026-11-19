"""
Staff discount limit (Business.max_staff_discount_cents) via PUT/GET
/business/settings. Souk e2e finding 2026-11-19: the limit (default 0) is
enforced by create_pos_order but nothing could set it. It binds MANAGERs as
well as STAFF, so only the OWNER may change it; changes are audited with
old/new values.
"""
import pytest
from pydantic import ValidationError

from backend.app.api.v1.routes_business import (
    MAX_STAFF_DISCOUNT_CEILING_CENTS,
    BusinessSettingsUpdate,
    get_business_settings,
    update_business_settings,
)
from backend.app.core.errors import AppError
from tests.test_business_settings_owner_only import FakeDB, FakeResult, make_business, make_user


async def test_get_settings_returns_the_limit():
    owner = make_user("OWNER")
    db = FakeDB([FakeResult(make_business(owner.business_id, max_staff_discount_cents=2500)), FakeResult(0)])
    assert (await get_business_settings(user=owner, db=db)).max_staff_discount_cents == 2500


async def test_owner_sets_the_limit_and_it_is_audited():
    owner = make_user("OWNER")
    business = make_business(owner.business_id)
    db = FakeDB([FakeResult(business)])
    out = await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=5000), user=owner, db=db)
    assert out.max_staff_discount_cents == 5000 and business.max_staff_discount_cents == 5000
    [event] = db.added
    assert event.action == "business.pos_limits.changed"
    assert event.business_id == owner.business_id
    assert event.diff_json["changes"] == {"max_staff_discount_cents": {"from": 0, "to": 5000}}


async def test_unchanged_limit_writes_no_audit():
    owner = make_user("OWNER")
    db = FakeDB([FakeResult(make_business(owner.business_id, max_staff_discount_cents=5000))])
    await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=5000), user=owner, db=db)
    assert db.added == []


async def test_explicit_null_is_ignored_not_written():
    owner = make_user("OWNER")
    business = make_business(owner.business_id, max_staff_discount_cents=700)
    db = FakeDB([FakeResult(business)])
    await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=None), user=owner, db=db)
    assert business.max_staff_discount_cents == 700 and db.added == []


@pytest.mark.parametrize("role", ["MANAGER", "STAFF"])
async def test_non_owner_cannot_change_the_limit(role):
    user = make_user(role)
    db = FakeDB([])  # refused before any DB lookup
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=100), user=user, db=db)
    assert exc.value.code == "OWNER_ONLY_FIELD" and exc.value.status_code == 403
    assert "max_staff_discount_cents" in exc.value.message and db.commits == 0


@pytest.mark.parametrize("bad", [-1, MAX_STAFF_DISCOUNT_CEILING_CENTS + 1])
def test_limit_is_validated(bad):
    with pytest.raises(ValidationError):
        BusinessSettingsUpdate(max_staff_discount_cents=bad)
