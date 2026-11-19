"""
The owner-set staff discount limit (PUT /business/settings
max_staff_discount_cents) is the one create_pos_order enforces. Real Postgres,
route functions called directly (souk e2e finding 2026-11-19: the limit could
not be set anywhere).
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.core.errors import AppError
from shared.models.audit import AuditEvent
from tests.integration.test_product_variants_realdb import _checkout, _matrix, _sell, _shop  # noqa: F401
from tests.integration.test_souk_security_review_realdb import _user, bag  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio


async def test_owner_set_limit_is_what_the_till_enforces(db, bag):
    biz, owner, item = await _shop(db, bag, price=10000)
    v = (await _matrix(db, owner, item))["LS-M-BLACK"]
    manager = _user(biz.id, role="MANAGER")

    # Default 0: a manager cannot discount at all.
    with pytest.raises(AppError) as exc:
        await _checkout(db, manager, [_sell(v.id, item_id=item.id)], discount_cents=1000)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"

    # A manager cannot raise their own limit.
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=5000), user=manager, db=db)
    assert exc.value.code == "OWNER_ONLY_FIELD"

    out = await update_business_settings(body=BusinessSettingsUpdate(max_staff_discount_cents=5000), user=owner, db=db)
    assert out.max_staff_discount_cents == 5000
    audit = (await db.execute(select(AuditEvent).where(
        AuditEvent.business_id == biz.id, AuditEvent.action == "business.pos_limits.changed"))).scalar_one()
    assert audit.diff_json["changes"] == {"max_staff_discount_cents": {"from": 0, "to": 5000}}

    order = await _checkout(db, manager, [_sell(v.id, item_id=item.id)], discount_cents=5000)
    assert order.discount_cents == 5000
    with pytest.raises(AppError) as exc:
        await _checkout(db, manager, [_sell(v.id, item_id=item.id)], discount_cents=5001)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"
