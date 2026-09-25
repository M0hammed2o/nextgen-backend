"""
Goods-receipt costing — real-Postgres proof (BUG-0111 fix, 2026-09-25).

Reads back the ACTUAL rows `create_goods_receipt` writes to `goods_receipts`,
`goods_receipt_lines`, and `stock_movements` against a real, migrated
database — not just the route's JSON response — so a mocked-session unit
test's assumptions about what SQLAlchemy actually persists can't hide a
discrepancy the way the original bug's three-different-numbers defect did.

Requires TEST_DATABASE_URL (and the fixtures in tests/integration/conftest.py)
per TESTING.md's "Integration-testing approach". Every business/ingredient
row uses a fresh uuid4-suffixed identifier so this file is safely
re-runnable, and test_business's fixture teardown removes it afterward.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_inventory import (
    GoodsReceiptCreateRequest,
    GoodsReceiptLineRequest,
    create_goods_receipt,
)
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.inventory import GoodsReceipt, GoodsReceiptLine, Ingredient, StockMovement


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


async def _make_ingredient(db: AsyncSession, business: Business, **overrides) -> Ingredient:
    defaults = dict(
        business_id=business.id,
        name=f"Test Cheese {uuid.uuid4().hex[:8]}",
        canonical_unit="GRAM",
        cost_per_canonical_micro_cents=0,
        purchase_pack_milli=1000,
    )
    defaults.update(overrides)
    ingredient = Ingredient(**defaults)
    db.add(ingredient)
    await db.commit()
    await db.refresh(ingredient)
    return ingredient


@pytest.fixture
async def inventory_business(test_business: Business, db: AsyncSession) -> Business:
    test_business.inventory_enabled = True
    db.add(test_business)
    await db.commit()
    await db.refresh(test_business)
    return test_business


async def test_5kg_at_r100_per_kg_matches_across_all_three_tables_in_real_db(
    inventory_business: Business, db: AsyncSession
):
    ingredient = await _make_ingredient(db, inventory_business, cost_per_canonical_micro_cents=999_000_000)
    user = _staff(inventory_business.id)

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="5", unit="KG", unit_cost_cents=10000,
        ),
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)
    receipt_id = uuid.UUID(result["id"])

    # Read back from a SEPARATE query, not the ORM objects still in the
    # session, to see exactly what Postgres actually has on disk.
    receipt = (
        await db.execute(select(GoodsReceipt).where(GoodsReceipt.id == receipt_id))
    ).scalar_one()
    line = (
        await db.execute(
            select(GoodsReceiptLine).where(GoodsReceiptLine.goods_receipt_id == receipt_id)
        )
    ).scalar_one()
    movement = (
        await db.execute(
            select(StockMovement).where(
                StockMovement.source_type == "GOODS_RECEIPT",
                StockMovement.source_id == receipt_id,
            )
        )
    ).scalar_one()

    assert receipt.total_cost_cents == 50000  # R500.00 — not 10000, not 110000
    assert line.line_cost_cents == 50000
    assert line.unit_cost_cents == 10000
    assert line.quantity_milli == 5_000_000
    assert movement.value_cents == 50000  # same R500.00 as the receipt/line
    assert movement.unit_cost_micro_cents == 10_000_000  # THIS delivery's rate, not
    # the ingredient's unrelated standing cost of 999_000_000 micro-cents/g


async def test_omitted_cost_falls_back_to_standing_cost_in_real_db(
    inventory_business: Business, db: AsyncSession
):
    ingredient = await _make_ingredient(db, inventory_business, cost_per_canonical_micro_cents=44_000_000)
    user = _staff(inventory_business.id)

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="500", unit="G"),
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)
    receipt_id = uuid.UUID(result["id"])

    receipt = (
        await db.execute(select(GoodsReceipt).where(GoodsReceipt.id == receipt_id))
    ).scalar_one()
    movement = (
        await db.execute(
            select(StockMovement).where(
                StockMovement.source_type == "GOODS_RECEIPT",
                StockMovement.source_id == receipt_id,
            )
        )
    ).scalar_one()

    assert receipt.total_cost_cents == 22000  # R220.00, not R0.00
    assert movement.value_cents == 22000


async def test_historical_receipt_immutable_to_later_ingredient_cost_change_in_real_db(
    inventory_business: Business, db: AsyncSession
):
    """Record a receipt, THEN change the ingredient's standing cost, and
    confirm the already-recorded receipt/line/movement rows in Postgres are
    untouched by a fresh read."""
    ingredient = await _make_ingredient(db, inventory_business, cost_per_canonical_micro_cents=50_000_000)
    user = _staff(inventory_business.id)

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(
            ingredient_id=ingredient.id, quantity="1", unit="KG", unit_cost_cents=8000,
        ),
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)
    receipt_id = uuid.UUID(result["id"])

    # Change the ingredient's standing cost after the fact.
    ingredient.cost_per_canonical_micro_cents = 999_000_000
    db.add(ingredient)
    await db.commit()

    receipt = (
        await db.execute(select(GoodsReceipt).where(GoodsReceipt.id == receipt_id))
    ).scalar_one()
    line = (
        await db.execute(
            select(GoodsReceiptLine).where(GoodsReceiptLine.goods_receipt_id == receipt_id)
        )
    ).scalar_one()
    movement = (
        await db.execute(
            select(StockMovement).where(
                StockMovement.source_type == "GOODS_RECEIPT",
                StockMovement.source_id == receipt_id,
            )
        )
    ).scalar_one()

    assert receipt.total_cost_cents == 8000
    assert line.line_cost_cents == 8000
    assert line.unit_cost_cents == 8000
    assert movement.value_cents == 8000
    assert movement.unit_cost_micro_cents == 8_000_000  # NOT the new 999_000_000
