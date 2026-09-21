"""
Receipt document — real-Postgres tests.

Mocked-session tests already cover pure rendering
(tests/test_inventory_pos_integration.py). These prove the things a mock
cannot honestly prove: that the document is built from an Order's own
stored snapshot regardless of what the live MenuItem/Business rows say
*now*, that cross-tenant access is refused at the DB layer, and that a
reprint is actually audited.

Requires a real DATABASE_URL, per tests/integration/conftest.py. Skips
(via the `db`/`business` fixtures raising at connection time, same as
every other file in this directory) if that DB is unreachable.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_receipts import (
    get_receipt_document,
    get_receipt_escpos,
    get_receipt_text,
    reprint_receipt,
)
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderItem
from shared.models.user import BusinessUser
from shared.pricing.engine import calculate_vat_cents
from shared.utils import generate_business_code


def _auth(business_id: uuid.UUID, role: str = "OWNER") -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name="Ciao Pizza",
        slug=f"ciao-pizza-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        address="1 Lifestyle Centre",
        phone="032 946 0000",
        vat_registered=True,
        vat_number="4123456789",
        vat_rate_basis_points=1500,
        vat_inclusive_pricing=True,
        receipt_paper_width_mm=80,
        receipt_copies=1,
        receipt_config_json={
            "trading_name": "Ciao Pizza Umhlanga",
            "branch_name": "Umhlanga",
            "footer_message": "Grazie!",
        },
    )
    defaults.update(overrides)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_cashier(db: AsyncSession, business: Business, staff_name="Thabo Staff") -> BusinessUser:
    user = BusinessUser(business_id=business.id, role="STAFF", staff_name=staff_name, pin_hash="x")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_order_with_item(
    db: AsyncSession, business: Business, *, menu_item: MenuItem | None = None, **order_overrides
) -> Order:
    """
    One line item: a "Large" size option (+R30), an "Extra Cheese" add-on
    (+R15), a removed "Mushroom" ingredient, a R4 staff discount with a
    reason, paid by cash with change due. Numbers are picked so tax_cents
    below is exactly what calculate_vat_cents() would derive at 15%
    inclusive — i.e. internally consistent, the way a real checkout leaves
    an order.
    """
    subtotal_cents = 24400
    discount_cents = 400
    delivery_fee_cents = 0
    net_total_cents = subtotal_cents - discount_cents + delivery_fee_cents
    vat_cents = calculate_vat_cents(net_total_cents, 1500, True)  # inclusive 15%
    total_cents = net_total_cents  # inclusive VAT never changes the total

    defaults = dict(
        business_id=business.id,
        order_number=f"BO-{uuid.uuid4().hex[:6].upper()}",
        status="COMPLETED",
        payment_status="PAID",
        order_mode="DINE_IN",
        source="MANUAL",
        subtotal_cents=subtotal_cents,
        delivery_fee_cents=delivery_fee_cents,
        total_cents=total_cents,
        currency="ZAR",
        customer_name="Walk-in",
        table_number="7",
        discount_cents=discount_cents,
        discount_reason="Loyalty customer",
        tax_cents=vat_cents,
        payment_method="CASH",
        cash_tendered_cents=25000,
        change_due_cents=25000 - total_cents,
    )
    defaults.update(order_overrides)
    order = Order(**defaults)
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id,
        business_id=business.id,
        menu_item_id=menu_item.id if menu_item else None,
        name_snapshot="Large Chicken Pizza",
        base_price_cents=19900,
        option_adjustment_cents=3000,
        add_on_total_cents=1500,
        unit_price_cents=24400,
        quantity=1,
        line_total_cents=24400,
        selected_options_snapshot=[
            {"group_id": "g1", "group_name": "Size", "option_id": "o1",
             "option_name": "Large", "price_delta_cents": 3000}
        ],
        add_ons_snapshot=[{"add_on_id": "a1", "name": "Extra Cheese", "price_cents": 1500, "quantity": 1}],
        removed_ingredients_snapshot=[{"id": "r1", "name": "Mushroom"}],
        special_instructions="Well done",
    )
    db.add(item)
    await db.commit()
    await db.refresh(order)
    return order


@pytest.fixture
async def business(db: AsyncSession) -> Business:
    biz = await _make_business(db)
    yield biz
    await db.execute(AuditEvent.__table__.delete().where(AuditEvent.business_id == biz.id))
    await db.execute(Order.__table__.delete().where(Order.business_id == biz.id))
    await db.execute(Business.__table__.delete().where(Business.id == biz.id))
    await db.commit()


class TestHistoricalIntegrity:
    """
    The whole point of storing snapshots: a receipt fetched today must show
    exactly what was charged, even after the menu price or the business's
    VAT configuration have since changed.
    """

    async def test_document_reflects_stored_snapshot_not_live_menu_or_vat_rate(
        self, db: AsyncSession, business: Business
    ):
        menu_item = MenuItem(business_id=business.id, name="Large Chicken Pizza", price_cents=99999)
        db.add(menu_item)
        await db.commit()
        await db.refresh(menu_item)

        cashier = await _make_cashier(db, business)
        order = await _make_order_with_item(
            db, business, menu_item=menu_item, created_by_user_id=cashier.id
        )

        auth = _auth(business.id)
        doc = (await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, auth, db))

        assert doc["header"]["trading_name"] == "Ciao Pizza Umhlanga"
        assert doc["header"]["branch_name"] == "Umhlanga"
        assert doc["header"]["vat_number"] == "4123456789"
        assert doc["footer"]["show_vat_invoice_label"] is True
        assert doc["meta"]["cashier_name"] == "Thabo Staff"
        assert doc["meta"]["reprint_count"] == 0
        assert doc["banner"] is None

        totals = doc["totals"]
        assert totals["subtotal_cents"] == 24400
        assert totals["subtotal_display"] == "R244.00"
        assert totals["discount_cents"] == 400
        assert totals["discount_display"] == "-R4.00"
        assert totals["discount_reason"] == "Loyalty customer"
        expected_vat = calculate_vat_cents(24000, 1500, True)
        assert totals["vat_cents"] == expected_vat
        assert totals["vat_display"] == f"R{expected_vat / 100:.2f}"
        assert totals["vat_rate_basis_points"] == 1500
        assert totals["vat_inclusive"] is True
        assert totals["total_cents"] == 24000
        assert totals["total_display"] == "R240.00"

        payment = doc["payment"]
        assert payment["cash_tendered_display"] == "R250.00"
        assert payment["change_due_display"] == "R10.00"

        line = doc["lines"][0]
        assert line["name"] == "Large Chicken Pizza"
        assert line["unit_amount_cents"] == 24400
        assert line["amount_display"] == "R244.00"
        modifier_labels = {m["label"] for m in line["modifiers"]}
        assert "Large" in modifier_labels
        assert "+ Extra Cheese" in modifier_labels
        assert "No Mushroom" in modifier_labels

        # Now change the live menu price AND the live VAT rate — neither
        # must move a single figure on a receipt for an order that already
        # happened.
        menu_item.price_cents = 555555
        business.vat_rate_basis_points = 2000  # was 1500 when the order was rung up
        await db.commit()

        doc2 = (await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, auth, db))
        totals2 = doc2["totals"]
        assert totals2["subtotal_cents"] == 24400
        assert totals2["vat_cents"] == expected_vat  # unchanged — from the snapshot
        assert totals2["vat_display"] == totals["vat_display"]
        assert totals2["total_cents"] == 24000
        # The live rate (20%) no longer reconciles with the stored VAT
        # amount computed at 15% — must be omitted, not shown wrong.
        assert totals2["vat_rate_basis_points"] is None
        assert totals2["vat_inclusive"] is None
        assert doc2["lines"][0]["amount_cents"] == 24400  # still the snapshot, not R5555.55


class TestTenantIsolation:
    async def test_cross_tenant_order_id_is_not_found(self, db: AsyncSession, business: Business):
        order = await _make_order_with_item(db, business)
        other = await _make_business(db, name="Other Restaurant")
        try:
            with pytest.raises(NotFoundError):
                await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, _auth(other.id), db)
        finally:
            await db.execute(AuditEvent.__table__.delete().where(AuditEvent.business_id == other.id))
            await db.execute(Order.__table__.delete().where(Order.business_id == other.id))
            await db.execute(Business.__table__.delete().where(Business.id == other.id))
            await db.commit()

    async def test_unknown_order_id_is_not_found(self, db: AsyncSession, business: Business):
        with pytest.raises(NotFoundError):
            await get_receipt_document(uuid.uuid4(), "CUSTOMER_RECEIPT", False, _auth(business.id), db)


class TestReprintAudit:
    async def test_reprint_is_audited_banner_and_counted(self, db: AsyncSession, business: Business):
        order = await _make_order_with_item(db, business)
        auth = _auth(business.id)

        first = await reprint_receipt(order.id, "CUSTOMER_RECEIPT", auth, db)
        assert first["banner"] == "REPRINT"
        assert first["meta"]["reprint_count"] == 1
        assert first["meta"]["last_reprinted_at"] is not None

        rows = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.business_id == business.id,
                    AuditEvent.action == "receipt.reprinted",
                    AuditEvent.target_id == order.id,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].scope == AuditScope.BUSINESS.value

        second = await reprint_receipt(order.id, "CUSTOMER_RECEIPT", auth, db)
        assert second["meta"]["reprint_count"] == 2

        # A plain GET after two reprints reports the history without
        # writing a third audit row itself.
        plain = await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, auth, db)
        assert plain["banner"] is None
        assert plain["meta"]["reprint_count"] == 2

        rows_after = (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.business_id == business.id,
                    AuditEvent.action == "receipt.reprinted",
                    AuditEvent.target_id == order.id,
                )
            )
        ).scalars().all()
        assert len(rows_after) == 2


class TestNonVatAndWhatsAppOrders:
    async def test_non_vat_registered_business_shows_no_vat_and_null_safe_cashier(
        self, db: AsyncSession
    ):
        biz = await _make_business(
            db, vat_registered=False, vat_number=None, vat_rate_basis_points=0,
        )
        try:
            order = await _make_order_with_item(
                db, biz, source="WHATSAPP", created_by_user_id=None,
                tax_cents=0, total_cents=24400, discount_cents=0, discount_reason=None,
                payment_method="CASH_ON_COLLECTION", cash_tendered_cents=None, change_due_cents=None,
            )
            doc = await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, _auth(biz.id), db)

            assert doc["header"]["vat_number"] is None
            assert doc["footer"]["show_vat_invoice_label"] is False
            assert doc["totals"]["vat_cents"] == 0
            assert doc["totals"]["vat_display"] is None
            assert doc["totals"]["vat_rate_basis_points"] is None
            assert doc["meta"]["cashier_name"] is None  # WhatsApp order: no created_by_user_id
            assert doc["banner"] is None
        finally:
            await db.execute(AuditEvent.__table__.delete().where(AuditEvent.business_id == biz.id))
            await db.execute(Order.__table__.delete().where(Order.business_id == biz.id))
            await db.execute(Business.__table__.delete().where(Business.id == biz.id))
            await db.commit()

    async def test_partially_refunded_order_shows_refund_banner_and_amount(
        self, db: AsyncSession, business: Business
    ):
        order = await _make_order_with_item(
            db, business,
            payment_status="PARTIALLY_REFUNDED", refund_amount_cents=1000,
        )
        doc = await get_receipt_document(order.id, "CUSTOMER_RECEIPT", False, _auth(business.id), db)
        assert doc["banner"] == "REFUNDED"
        assert doc["payment"]["refund_amount_cents"] == 1000
        assert doc["payment"]["refund_amount_display"] == "R10.00"


class TestTextAndEscposParityAtBothWidths:
    async def test_text_and_escpos_endpoints_never_overflow_58mm_with_a_long_item_name(
        self, db: AsyncSession, business: Business
    ):
        business.receipt_paper_width_mm = 58
        await db.commit()
        order = await _make_order_with_item(db, business)
        # Overwrite the item's name with something absurdly long, the way a
        # real long combo/allergen-laden name could be.
        items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
        items[0].name_snapshot = "X" * 80
        await db.commit()

        auth = _auth(business.id)
        text_response = await get_receipt_text(order.id, "CUSTOMER_RECEIPT", False, auth, db)
        for line in text_response.body.decode("utf-8").split("\n"):
            assert len(line) <= 32, f"58mm line overflowed: {line!r}"

        escpos_response = await get_receipt_escpos(
            order.id, "CUSTOMER_RECEIPT", False, False, True, True, True, "cp437", auth, db
        )
        assert escpos_response.body.startswith(b"\x1b@")
