"""
Real-Postgres tests for product variants (migration 0066): matrix creation,
per-business SKU/barcode uniqueness, tenant isolation of variant ids, price
override, VARIANT_REQUIRED, per-variant stock on sale / refund / exchange,
discount pro-rating on a line return, and variant reporting (no double count).
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.analytics import product_rankings, sales_wallets, variant_report
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSRefundRequest,
    POSReturnLine,
    TillOpenRequest,
    create_pos_order,
    open_till,
    refund_pos_order,
)
from backend.app.api.v1.routes_variants import (
    MatrixCreate,
    VariantCreate,
    VariantUpdate,
    create_variant,
    create_variant_matrix,
    deactivate_variant,
    list_variants,
    pos_lookup,
    update_variant,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.inventory.ledger import stock_on_hand
from shared.models.order import Order, OrderItem
from shared.models.variant import ProductVariant
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    _event,
    _plain_business,
    _pay,
    _row,
    _user,
    _vendors,
    DAYS,
    bag,
)

pytestmark = pytest.mark.asyncio


# -- helpers -------------------------------------------------------------------

async def _shop(db, bag, price=10000):
    """A POS+inventory business with an open till and a 'Linen Shirt' item."""
    biz = await _plain_business(db, bag, pos_enabled=True, inventory_enabled=True)
    owner = _user(biz.id)
    cat = await create_category(body=CategoryCreate(name="Shirts"), user=owner, db=db)
    item = await create_item(body=ItemCreate(category_id=cat.id, name="Linen Shirt", price_cents=price),
                             user=owner, db=db)
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    return biz, owner, item


async def _matrix(db, owner, item, **kw):
    kw.setdefault("sku_prefix", "LS")
    kw.setdefault("sizes", ["S", "M", "L"])
    kw.setdefault("colours", ["Black", "White"])
    kw.setdefault("opening_stock", 5)
    kw.setdefault("cost_cents", 4000)
    out = await create_variant_matrix(item.id, MatrixCreate(**kw), owner, db)
    return {v.sku: v for v in out}


def _sell(variant_id, qty=1, item_id=None, **kw):
    return POSOrderItemRequest(menu_item_id=item_id, variant_id=variant_id, quantity=qty, **kw)


async def _checkout(db, owner, lines, **kw):
    return await create_pos_order(
        POSOrderCreateRequest(items=lines, payment_method="CASH", cash_tendered_cents=10_000_000, **kw), owner, db)


async def _stock(db, biz, *variants):
    ing = [v.ingredient_id for v in variants]
    on_hand = await stock_on_hand(db, biz.id, ingredient_ids=ing)
    return [int(on_hand.get(i, 0)) // 1000 for i in ing]


async def _code(coro):
    with pytest.raises((AppError, NotFoundError)) as exc:
        await coro
    return getattr(exc.value, "code", None) or getattr(exc.value, "error_code", None), exc.value.status_code


# -- matrix / uniqueness -------------------------------------------------------

async def test_matrix_creates_size_by_colour_variants_with_stock(db, bag):
    biz, owner, item = await _shop(db, bag)
    variants = await _matrix(db, owner, item)
    assert sorted(variants) == sorted(f"LS-{s}-{c}" for s in "SML" for c in ("BLACK", "WHITE"))
    v = variants["LS-M-BLACK"]
    assert v.attributes == {"size": "M", "colour": "Black"} and v.label == "M / Black"
    assert v.effective_price_cents == 10000 and v.cost_cents == 4000 and v.stock_on_hand == 5
    listed = await list_variants(item.id, False, owner, db)
    assert len(listed) == 6 and all(x.stock_on_hand == 5 for x in listed)


async def test_matrix_is_all_or_nothing_on_sku_clash(db, bag):
    biz, owner, item = await _shop(db, bag)
    await create_variant(item.id, VariantCreate(sku="LS-M-BLACK", attributes={"size": "M"}), owner, db)
    assert (await _code(_matrix(db, owner, item)))[1] == 409
    rows = (await db.execute(select(ProductVariant).where(ProductVariant.business_id == biz.id))).scalars().all()
    assert len(rows) == 1  # nothing from the failed matrix leaked in


async def test_sku_and_barcode_unique_per_business_but_reusable_across_businesses(db, bag):
    biz, owner, item = await _shop(db, bag)
    other, other_owner, other_item = await _shop(db, bag)
    body = dict(sku="TEE-1", barcode="6001234567890", attributes={"size": "M"})
    await create_variant(item.id, VariantCreate(**body), owner, db)
    assert (await _code(create_variant(item.id, VariantCreate(**{**body, "barcode": "111111"}), owner, db)))[0] == "SKU_TAKEN"
    assert (await _code(create_variant(item.id, VariantCreate(**{**body, "sku": "TEE-2"}), owner, db)))[0] == "BARCODE_TAKEN"
    # The very same SKU and barcode are fine in a different business.
    again = await create_variant(other_item.id, VariantCreate(**body), other_owner, db)
    assert again.sku == "TEE-1" and again.barcode == "6001234567890"


async def test_variant_input_validation():
    with pytest.raises(ValidationError):
        VariantCreate(sku="  ", attributes={"size": "M"})
    with pytest.raises(ValidationError):
        VariantCreate(sku="A", attributes={})
    with pytest.raises(ValidationError):
        VariantCreate(sku="A", attributes={"size": "M"}, price_cents=-1)
    with pytest.raises(ValidationError):
        MatrixCreate(sku_prefix="A", sizes=[str(i) for i in range(21)], colours=[str(i) for i in range(11)])
    with pytest.raises(ValidationError):
        MatrixCreate(sku_prefix="A")


# -- tenant isolation ----------------------------------------------------------

async def test_cross_tenant_variant_cannot_be_sold_or_read_or_edited(db, bag):
    biz, owner, item = await _shop(db, bag)
    other, other_owner, other_item = await _shop(db, bag)
    theirs = await _matrix(db, other_owner, other_item)
    mine = await _matrix(db, owner, item)

    # Selling the other business's variant, on my own item or on theirs: 404, nothing created.
    for menu_item in (item.id, other_item.id):
        assert (await _code(_checkout(db, owner, [_sell(theirs["LS-M-BLACK"].id, item_id=menu_item)])))[1] == 404
    # A variant of a different item in MY business is also refused.
    cat = await create_category(body=CategoryCreate(name="Trousers"), user=owner, db=db)
    pants = await create_item(body=ItemCreate(category_id=cat.id, name="Chinos", price_cents=20000), user=owner, db=db)
    assert (await _code(_checkout(db, owner, [_sell(mine["LS-M-BLACK"].id, item_id=pants.id)])))[1] == 404
    assert (await db.execute(select(Order).where(Order.business_id == biz.id))).first() is None
    assert await _stock(db, biz, mine["LS-M-BLACK"]) == [5]

    # Reads / writes of the other business's item or variant are 404.
    assert (await _code(list_variants(other_item.id, False, owner, db)))[1] == 404
    assert (await _code(update_variant(theirs["LS-M-BLACK"].id, VariantUpdate(price_cents=1), owner, db)))[1] == 404
    assert (await _code(deactivate_variant(theirs["LS-M-BLACK"].id, owner, db)))[1] == 404
    assert (await _code(create_variant(other_item.id, VariantCreate(sku="X", attributes={"a": "b"}), owner, db)))[1] == 404


async def test_barcode_lookup_is_business_scoped(db, bag):
    biz, owner, item = await _shop(db, bag)
    other, other_owner, other_item = await _shop(db, bag)
    v = await create_variant(item.id, VariantCreate(sku="A-1", barcode="9990001", attributes={"size": "M"}), owner, db)
    hit = await pos_lookup("9990001", None, owner, db)
    assert hit.variant.id == v.id and hit.menu_item_id == item.id and hit.base_price_cents == 10000
    by_sku = await pos_lookup(None, "A-1", owner, db)
    assert by_sku.variant.id == v.id
    assert (await _code(pos_lookup("9990001", None, other_owner, db)))[1] == 404  # other tenant
    assert (await _code(pos_lookup("0000", None, owner, db)))[1] == 404
    assert (await _code(pos_lookup(None, None, owner, db)))[1] == 422
    await deactivate_variant(v.id, owner, db)
    assert (await _code(pos_lookup("9990001", None, owner, db)))[1] == 404  # inactive is not scannable


# -- pricing -------------------------------------------------------------------

async def test_price_override_vs_item_base_price(db, bag):
    biz, owner, item = await _shop(db, bag, price=10000)
    plain = await create_variant(item.id, VariantCreate(sku="P-S", attributes={"size": "S"}), owner, db)
    pricey = await create_variant(item.id, VariantCreate(sku="P-XL", attributes={"size": "XL"}, price_cents=12500), owner, db)
    order = await _checkout(db, owner, [_sell(plain.id, 2, item.id), _sell(pricey.id, 1, item.id)])
    by_sku = {i.sku_snapshot: i for i in order.items}
    assert by_sku["P-S"].unit_price_cents == 10000 and by_sku["P-S"].line_total_cents == 20000
    assert by_sku["P-XL"].unit_price_cents == 12500 and by_sku["P-XL"].variant_label_snapshot == "XL"
    assert order.subtotal_cents == 32500 and order.total_cents == 32500
    # Clearing the override reverts to the base price for later sales only.
    await update_variant(pricey.id, VariantUpdate(price_cents=None), owner, db)
    later = await _checkout(db, owner, [_sell(pricey.id, 1, item.id)])
    assert later.items[0].unit_price_cents == 10000
    assert by_sku["P-XL"].unit_price_cents == 12500


async def test_item_with_variants_requires_one_and_inactive_is_refused(db, bag):
    biz, owner, item = await _shop(db, bag)
    v = await create_variant(item.id, VariantCreate(sku="R-M", attributes={"size": "M"}), owner, db)
    assert await _code(_checkout(db, owner, [POSOrderItemRequest(menu_item_id=item.id, quantity=1)])) == ("VARIANT_REQUIRED", 422)
    await deactivate_variant(v.id, owner, db)
    # With its only variant inactive the item has no active variants (sellable plain) but the
    # inactive id itself must be refused.
    assert await _code(_checkout(db, owner, [_sell(v.id, 1, item.id)])) == ("VARIANT_INACTIVE", 422)


async def test_restaurant_item_with_option_groups_is_priced_exactly_as_before(db, bag):
    biz, owner, _ = await _shop(db, bag)
    opts = {"option_groups": [{
        "id": "size", "name": "Size", "required": True, "min_selections": 1, "max_selections": 1,
        "sort_order": 0, "is_enabled": True, "default_option_id": None,
        "options": [{"id": "reg", "name": "Regular", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                    {"id": "lrg", "name": "Large", "price_delta_cents": 1500, "sort_order": 1, "is_enabled": True}],
    }]}
    cat = await create_category(body=CategoryCreate(name="Mains"), user=owner, db=db)
    pizza = await create_item(body=ItemCreate(category_id=cat.id, name="Pizza", price_cents=9000, options_json=opts),
                              user=owner, db=db)
    order = await _checkout(db, owner, [POSOrderItemRequest(menu_item_id=pizza.id, quantity=2,
                                                            selected_option_ids={"size": "lrg"})])
    line = order.items[0]
    assert (line.unit_price_cents, line.line_total_cents, order.total_cents) == (10500, 21000, 21000)
    assert line.variant_id is None and line.sku_snapshot is None
    # Supplying a variant id on an item that has none is a 404, never silently ignored.
    assert (await _code(_checkout(db, owner, [POSOrderItemRequest(
        menu_item_id=pizza.id, quantity=1, variant_id=uuid.uuid4(), selected_option_ids={"size": "reg"})])))[1] == 404


# -- stock: sale / refund / exchange ------------------------------------------------

async def test_sale_deducts_only_that_variants_stock(db, bag):
    biz, owner, item = await _shop(db, bag)
    vs = await _matrix(db, owner, item)
    await _checkout(db, owner, [_sell(vs["LS-M-BLACK"].id, 2, item.id)])
    assert await _stock(db, biz, vs["LS-M-BLACK"], vs["LS-M-WHITE"], vs["LS-L-BLACK"], vs["LS-S-WHITE"]) == [3, 5, 5, 5]
    listed = {v.sku: v.stock_on_hand for v in await list_variants(item.id, False, owner, db)}
    assert listed["LS-M-BLACK"] == 3 and listed["LS-L-WHITE"] == 5


async def test_full_refund_restores_stock_only_when_asked(db, bag):
    biz, owner, item = await _shop(db, bag)
    v = (await _matrix(db, owner, item))["LS-M-BLACK"]
    kept = await _checkout(db, owner, [_sell(v.id, 1, item.id)])
    await refund_pos_order(kept.id, POSRefundRequest(reason="changed mind"), owner, db)
    assert await _stock(db, biz, v) == [4]  # money back, goods not (restore_stock defaults False)
    back = await _checkout(db, owner, [_sell(v.id, 2, item.id)])
    assert await _stock(db, biz, v) == [2]
    await refund_pos_order(back.id, POSRefundRequest(reason="unworn", restore_stock=True), owner, db)
    assert await _stock(db, biz, v) == [4]


async def test_exchange_end_to_end_return_m_take_l(db, bag):
    biz, owner, item = await _shop(db, bag)
    vs = await _matrix(db, owner, item, sizes=["M", "L"], colours=["Black"])
    m, l = vs["LS-M-BLACK"], vs["LS-L-BLACK"]
    await update_variant(l.id, VariantUpdate(price_cents=11000), owner, db)
    sale = await _checkout(db, owner, [_sell(m.id, 1, item.id)])
    assert (await _stock(db, biz, m, l)) == [4, 5]

    # Step 1: refund the returned line (stock back); step 2: ring up the replacement, linked.
    line = sale.items[0]
    refunded = await refund_pos_order(
        sale.id, POSRefundRequest(reason="wrong size", restore_stock=True,
                                  return_items=[POSReturnLine(order_item_id=line.id, quantity=1)]), owner, db)
    assert refunded.refund_amount_cents == 10000 and refunded.payment_status == "REFUNDED"
    assert (await _stock(db, biz, m, l)) == [5, 5]  # M +1

    exch = await _checkout(db, owner, [_sell(l.id, 1, item.id)], exchange_of_order_id=sale.id)
    assert exch.exchange_of_order_id == sale.id and exch.total_cents == 11000
    assert (await _stock(db, biz, m, l)) == [5, 4]  # L -1
    # Money: customer paid 10000, got 10000 back, paid 11000 -> net 11000 kept, one pair of L gone.
    assert (await db.execute(select(OrderItem.returned_quantity).where(OrderItem.id == line.id))).scalar_one() == 1


async def test_exchange_link_must_point_at_a_refunded_order_of_the_same_business(db, bag):
    biz, owner, item = await _shop(db, bag)
    other, other_owner, other_item = await _shop(db, bag)
    v = await create_variant(item.id, VariantCreate(sku="E-M", attributes={"size": "M"}, opening_stock=3), owner, db)
    ov = await create_variant(other_item.id, VariantCreate(sku="E-O", attributes={"size": "M"}), other_owner, db)
    paid = await _checkout(db, owner, [_sell(v.id, 1, item.id)])
    assert (await _code(_checkout(db, owner, [_sell(v.id, 1, item.id)], exchange_of_order_id=paid.id)))[0] \
        == "EXCHANGE_ORIGINAL_NOT_REFUNDED"
    foreign = await _checkout(db, other_owner, [_sell(ov.id, 1, other_item.id)])
    await refund_pos_order(foreign.id, POSRefundRequest(reason="x"), other_owner, db)
    assert (await _code(_checkout(db, owner, [_sell(v.id, 1, item.id)], exchange_of_order_id=foreign.id)))[1] == 404


async def test_partial_line_return_prorates_discount_and_restocks_only_that_line(db, bag):
    biz, owner, item = await _shop(db, bag, price=5000)
    vs = await _matrix(db, owner, item, sizes=["M", "L"], colours=["Black"])
    m, l = vs["LS-M-BLACK"], vs["LS-L-BLACK"]
    sale = await _checkout(db, owner, [_sell(m.id, 1, item.id), _sell(l.id, 1, item.id)], discount_cents=1000)
    assert (sale.subtotal_cents, sale.total_cents) == (10000, 9000)
    m_line = next(i for i in sale.items if i.sku_snapshot == "LS-M-BLACK")
    over = POSRefundRequest(reason="r", return_items=[POSReturnLine(order_item_id=m_line.id, quantity=2)])
    assert await _code(refund_pos_order(sale.id, over, owner, db)) == ("RETURN_EXCEEDS_SOLD", 422)
    too_much = POSRefundRequest(reason="r", amount_cents=5000, restore_stock=True,
                                return_items=[POSReturnLine(order_item_id=m_line.id, quantity=1)])
    assert (await _code(refund_pos_order(sale.id, too_much, owner, db)))[0] == "REFUND_EXCEEDS_RETURNED_VALUE"
    out = await refund_pos_order(
        sale.id, POSRefundRequest(reason="r", restore_stock=True,
                                  return_items=[POSReturnLine(order_item_id=m_line.id, quantity=1)]), owner, db)
    assert out.refund_amount_cents == 4500 and out.payment_status == "PARTIALLY_REFUNDED"  # 5000 x 9000/10000
    assert await _stock(db, biz, m, l) == [5, 4]  # M restored, L still sold


async def test_amount_only_partial_refund_cannot_restore_variant_stock_blindly(db, bag):
    biz, owner, item = await _shop(db, bag)
    v = await create_variant(item.id, VariantCreate(sku="B-M", attributes={"size": "M"}, opening_stock=3), owner, db)
    sale = await _checkout(db, owner, [_sell(v.id, 1, item.id)])
    body = POSRefundRequest(amount_cents=1000, reason="goodwill", restore_stock=True)
    assert await _code(refund_pos_order(sale.id, body, owner, db)) == ("RETURN_ITEMS_REQUIRED", 422)
    assert await _stock(db, biz, v) == [2]


# -- trading gate ---------------------------------------------------------------

async def test_variant_sale_goes_through_the_souk_trading_gate(db, bag):
    ev = await _event(db, bag)
    [vend] = await _vendors(db, ev, bag, [_row(bag)])  # no paid trading day yet
    owner = _user(vend["business_id"])
    cat = await create_category(body=CategoryCreate(name="Tops"), user=owner, db=db)
    item = await create_item(body=ItemCreate(category_id=cat.id, name="Tee", price_cents=5000), user=owner, db=db)
    v = await create_variant(item.id, VariantCreate(sku="G-M", attributes={"size": "M"}), owner, db)
    assert (await _code(_checkout(db, owner, [_sell(v.id, 1, item.id)])))[0] == "EVENT_TRADING_NOT_ALLOWED"


# -- reporting ---------------------------------------------------------------------

async def test_variant_report_is_not_double_counted_and_rankings_still_work(db, bag):
    biz, owner, item = await _shop(db, bag, price=5000)
    vs = await _matrix(db, owner, item, sizes=["M", "L"], colours=["Black"])
    m, l = vs["LS-M-BLACK"], vs["LS-L-BLACK"]
    a = await _checkout(db, owner, [_sell(m.id, 2, item.id), _sell(l.id, 1, item.id)])
    b = await _checkout(db, owner, [_sell(m.id, 1, item.id)])
    await refund_pos_order(b.id, POSRefundRequest(reason="r", restore_stock=True,
                                                  return_items=[POSReturnLine(order_item_id=b.items[0].id, quantity=1)]),
                           owner, db)
    rng = sales_wallets.resolve_range("today", None, None, "Africa/Johannesburg")
    rep = await variant_report.variant_sales(db, biz.id, rng)
    row = {v["sku"]: v for v in rep["variants"]}
    assert (row["LS-M-BLACK"]["units_sold"], row["LS-M-BLACK"]["units_returned"], row["LS-M-BLACK"]["net_units"]) == (3, 1, 2)
    assert (row["LS-M-BLACK"]["gross_cents"], row["LS-M-BLACK"]["net_cents"]) == (15000, 10000)
    assert (row["LS-L-BLACK"]["units_sold"], row["LS-L-BLACK"]["net_cents"]) == (1, 5000)
    assert rep["totals"]["units_sold"] == 4 and rep["totals"]["net_cents"] == 15000
    # Same number of lines in the DB as rows summed: nothing counted twice.
    lines = (await db.execute(select(OrderItem.quantity).where(OrderItem.business_id == biz.id))).scalars().all()
    assert sum(lines) == rep["totals"]["units_sold"]
    # Product rankings group by the item and keep working with variant lines.
    ranks = await product_rankings.best_sellers(db, biz.id, rng)
    top = ranks["channels"][0] if "channels" in ranks else ranks
    assert str(item.id) in repr(ranks)


async def test_other_business_variants_never_appear_in_my_report(db, bag):
    biz, owner, item = await _shop(db, bag)
    other, other_owner, other_item = await _shop(db, bag)
    ov = await create_variant(other_item.id, VariantCreate(sku="O-M", attributes={"size": "M"}), other_owner, db)
    await _checkout(db, other_owner, [_sell(ov.id, 1, other_item.id)])
    rng = sales_wallets.resolve_range("today", None, None, "Africa/Johannesburg")
    assert (await variant_report.variant_sales(db, biz.id, rng))["variants"] == []
