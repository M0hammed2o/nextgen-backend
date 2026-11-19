"""
Per-variant stock adjustment through the existing manual-adjustment endpoint
(POST /business/inventory/adjustments), which the manager's variant row now
uses (souk e2e finding 2026-11-19: per-variant stock could not be edited
after creation). Proves the contract the UI relies on, against real Postgres:
unit "unit" on a variant's UNIT ingredient, a signed change, audited, and
scoped to the caller's business only.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.api.v1.routes_inventory import AdjustmentRequest, create_adjustment
from backend.app.api.v1.routes_variants import list_variants
from backend.app.core.errors import AppError, NotFoundError
from shared.models.audit import AuditEvent
from tests.integration.test_product_variants_realdb import _matrix, _shop  # noqa: F401
from tests.integration.test_souk_security_review_realdb import _user, bag  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio


async def _stock(db, owner, item):
    return {v.sku: v.stock_on_hand for v in await list_variants(item.id, False, owner, db)}


async def test_signed_unit_adjustment_changes_one_variant_and_is_audited(db, bag):
    biz, owner, item = await _shop(db, bag)
    variants = await _matrix(db, owner, item)  # 6 variants, 5 each
    target = variants["LS-M-BLACK"]
    out = await create_adjustment(
        AdjustmentRequest(ingredient_id=target.ingredient_id, quantity="3", unit="unit", reason="Stock count correction"),
        owner, db)
    assert out["quantity_milli"] == 3000
    stock = await _stock(db, owner, item)
    assert stock["LS-M-BLACK"] == 8
    assert all(q == 5 for sku, q in stock.items() if sku != "LS-M-BLACK")

    await create_adjustment(
        AdjustmentRequest(ingredient_id=target.ingredient_id, quantity="-2", unit="unit", reason="Damaged"),
        _user(biz.id, role="MANAGER"), db)
    assert (await _stock(db, owner, item))["LS-M-BLACK"] == 6

    audits = (await db.execute(select(AuditEvent).where(
        AuditEvent.business_id == biz.id, AuditEvent.action == "inventory.stock.adjusted",
        AuditEvent.target_id == target.ingredient_id))).scalars().all()
    assert sorted(a.diff_json["quantity_milli"] for a in audits) == [-2000, 3000]


async def test_another_business_cannot_adjust_this_variant(db, bag):
    _, owner, item = await _shop(db, bag)
    target = (await _matrix(db, owner, item))["LS-S-WHITE"]
    other_biz, other_owner, _ = await _shop(db, bag)
    with pytest.raises(NotFoundError):
        await create_adjustment(
            AdjustmentRequest(ingredient_id=target.ingredient_id, quantity="10", unit="unit", reason="Not mine"),
            other_owner, db)
    assert (await _stock(db, owner, item))["LS-S-WHITE"] == 5


async def test_zero_change_is_refused(db, bag):
    _, owner, item = await _shop(db, bag)
    target = (await _matrix(db, owner, item))["LS-L-BLACK"]
    with pytest.raises(AppError) as exc:
        await create_adjustment(
            AdjustmentRequest(ingredient_id=target.ingredient_id, quantity="0", unit="unit", reason="Nothing"),
            owner, db)
    assert exc.value.code == "INVALID_QUANTITY"
