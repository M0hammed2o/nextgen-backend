"""
Real-Postgres tests for `has_variants` on the menu item responses.

The staff till used to call GET /menu/items/{id}/variants for every item it
showed or that was tapped, even plain restaurant products. The list (and the
create/update responses) now say whether an item has ACTIVE variants, which is
the same rule the POS uses for VARIANT_REQUIRED and the same set the variants
GET returns by default. These tests pin: true/false, inactive variants not
counting, tenant isolation of the flag, and that the list query does not grow
with the number of items (no N+1).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from backend.app.api.v1.routes_menu import (
    CategoryCreate,
    ItemCreate,
    ItemUpdate,
    create_category,
    create_item,
    list_items,
    update_item,
)
from backend.app.api.v1.routes_variants import (
    MatrixCreate,
    VariantCreate,
    VariantUpdate,
    create_variant,
    create_variant_matrix,
    deactivate_variant,
    update_variant,
)
from shared.models.variant import ProductVariant
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    _plain_business,
    _user,
    bag,
)

pytestmark = pytest.mark.asyncio


async def _shop(db, bag):
    biz = await _plain_business(db, bag, pos_enabled=True, inventory_enabled=True)
    owner = _user(biz.id)
    cat = await create_category(body=CategoryCreate(name="Stuff"), user=owner, db=db)
    return biz, owner, cat


async def _item(db, owner, cat, name):
    return await create_item(body=ItemCreate(category_id=cat.id, name=name, price_cents=5000), user=owner, db=db)


async def _flags(db, user, include_inactive=False):
    rows = await list_items(category_id=None, include_inactive=include_inactive, user=user, db=db)
    return {r.name: r.has_variants for r in rows}


async def test_plain_item_false_variant_item_true(db, bag):
    biz, owner, cat = await _shop(db, bag)
    burger = await _item(db, owner, cat, "Burger")
    shirt = await _item(db, owner, cat, "Shirt")
    assert burger.has_variants is False and shirt.has_variants is False  # create response

    await create_variant_matrix(
        shirt.id, MatrixCreate(sku_prefix="SH", sizes=["S", "M"], colours=["Black"], track_stock=False), owner, db)

    assert await _flags(db, owner) == {"Burger": False, "Shirt": True}
    staff = _user(biz.id, role="STAFF")
    assert await _flags(db, staff) == {"Burger": False, "Shirt": True}  # the till's role sees the same

    # The update response carries the flag too.
    updated = await update_item(item_id=shirt.id, body=ItemUpdate(description="Linen"), user=owner, db=db)
    assert updated.has_variants is True
    updated = await update_item(item_id=burger.id, body=ItemUpdate(description="Beef"), user=owner, db=db)
    assert updated.has_variants is False


async def test_inactive_variants_do_not_count(db, bag):
    """Matches the contract: VARIANT_REQUIRED and the default variants GET only consider active variants."""
    biz, owner, cat = await _shop(db, bag)
    shirt = await _item(db, owner, cat, "Shirt")
    a = await create_variant(shirt.id, VariantCreate(sku="SH-A", attributes={"size": "S"}, track_stock=False), owner, db)
    b = await create_variant(shirt.id, VariantCreate(sku="SH-B", attributes={"size": "M"}, track_stock=False), owner, db)
    assert (await _flags(db, owner))["Shirt"] is True

    await deactivate_variant(a.id, owner, db)
    assert (await _flags(db, owner))["Shirt"] is True  # one active left

    await update_variant(b.id, VariantUpdate(is_active=False), owner, db)
    assert (await _flags(db, owner))["Shirt"] is False  # none active
    assert (await update_item(item_id=shirt.id, body=ItemUpdate(sort_order=1), user=owner, db=db)).has_variants is False

    await update_variant(b.id, VariantUpdate(is_active=True), owner, db)
    assert (await _flags(db, owner))["Shirt"] is True  # reactivated


async def test_flag_is_tenant_scoped(db, bag):
    biz, owner, cat = await _shop(db, bag)
    other, other_owner, other_cat = await _shop(db, bag)
    mine = await _item(db, owner, cat, "Mine")
    theirs = await _item(db, other_owner, other_cat, "Theirs")
    await create_variant(theirs.id, VariantCreate(sku="T-1", attributes={"size": "M"}, track_stock=False),
                         other_owner, db)

    # Each business sees only its own items, and the other business's variants never leak in.
    assert await _flags(db, owner) == {"Mine": False}
    assert await _flags(db, other_owner) == {"Theirs": True}

    # Defence in depth: even a (normally impossible) variant row whose business_id differs from the
    # item's business does not flip the item's flag.
    db.add(ProductVariant(business_id=other.id, menu_item_id=mine.id, sku=f"X-{uuid.uuid4().hex[:6]}",
                          attributes={"size": "M"}, is_active=True))
    await db.commit()
    assert await _flags(db, owner) == {"Mine": False}


async def test_list_query_count_does_not_grow_with_items(db, bag):
    biz, owner, cat = await _shop(db, bag)

    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    sync_engine = (await db.connection()).engine.sync_engine

    async def _measure():
        statements.clear()
        event.listen(sync_engine, "before_cursor_execute", _count)
        try:
            rows = await list_items(category_id=None, include_inactive=False, user=owner, db=db)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)
        return rows, len(statements)

    for i in range(3):
        await _item(db, owner, cat, f"Small {i}")
    first = await _item(db, owner, cat, "Small V")
    await create_variant(first.id, VariantCreate(sku="SV-1", attributes={"size": "M"}, track_stock=False), owner, db)
    small_rows, small_count = await _measure()
    assert len(small_rows) == 4

    for i in range(30):
        it = await _item(db, owner, cat, f"Big {i:02d}")
        if i % 3 == 0:
            await create_variant(it.id, VariantCreate(sku=f"BV-{i}", attributes={"size": "M"}, track_stock=False),
                                 owner, db)
    big_rows, big_count = await _measure()
    assert len(big_rows) == 34
    assert sum(r.has_variants for r in big_rows) == 1 + 10

    # One SELECT for the items + their has_variants flag, one selectin load for add-ons: constant.
    assert big_count == small_count
    assert big_count <= 2, statements
    assert any("EXISTS" in s.upper() and "product_variants" in s for s in statements)
