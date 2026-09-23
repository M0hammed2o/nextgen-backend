"""
Public digital receipt page — real-Postgres proofs.

GET /v1/r/{token} (backend/app/api/v1/routes_receipt_public.py) is called
here via its own `__wrapped__` attribute (functools.wraps sets this
automatically under the @limiter.limit decorator) — this exercises the
REAL route function/business logic against a real DB without needing a
live Redis for slowapi's rate-limit storage backend (unavailable in this
sandbox; see TestRateLimitConfiguration below for what IS verified about
rate limiting without one, and the task report for what is explicitly
Unknown/unverified as a result).

Requires TEST_DATABASE_URL to point at a reachable real Postgres migrated
to at least 0039_digital_receipts (see docker-compose.test.yml /
TESTING.md). Fails loudly if unreachable/unset, via the `db` fixture below
(same pattern as tests/integration/test_receipts_realdb.py).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from backend.app.api.v1.routes_receipt_public import get_public_receipt
from backend.app.core.ratelimit import limiter
from backend.app.core.config import get_settings
from backend.app.receipts.whatsapp_receipt import send_digital_receipt
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.digital_receipt import DigitalReceipt
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderItem
from shared.utils import generate_business_code

_HANDLER = get_public_receipt.__wrapped__
settings = get_settings()


async def _make_business(db, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name="Public Receipt Test Restaurant",
        slug=f"public-receipt-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        digital_receipts_enabled=True,
        whatsapp_phone_number_id=f"1{uuid.uuid4().int % 10**9:09d}",
        is_whatsapp_enabled=True,
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True,
    )
    defaults.update(overrides)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_order_with_item(db, business, menu_item=None, **overrides) -> Order:
    defaults = dict(
        business_id=business.id,
        order_number=f"BO-{uuid.uuid4().hex[:8].upper()}",
        status="COMPLETED",
        payment_status="PAID",
        source="MANUAL",
        subtotal_cents=24400,
        total_cents=24000,
        tax_cents=calculate_test_vat(),
        currency="ZAR",
    )
    defaults.update(overrides)
    order = Order(**defaults)
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id,
        business_id=business.id,
        menu_item_id=menu_item.id if menu_item else None,
        name_snapshot="Large Chicken Pizza",
        unit_price_cents=24400,
        quantity=1,
        line_total_cents=24400,
    )
    db.add(item)
    await db.commit()
    await db.refresh(order)
    return order


def calculate_test_vat() -> int:
    from shared.pricing.engine import calculate_vat_cents
    return calculate_vat_cents(24000, 1500, True)


async def _cleanup(db, business: Business):
    await db.execute(delete(DigitalReceipt).where(DigitalReceipt.business_id == business.id))
    await db.execute(delete(OrderItem).where(OrderItem.business_id == business.id))
    await db.execute(delete(Order).where(Order.business_id == business.id))
    await db.execute(delete(Customer).where(Customer.business_id == business.id))
    await db.execute(delete(MenuItem).where(MenuItem.business_id == business.id))
    await db.execute(delete(Business).where(Business.id == business.id))
    await db.commit()


@pytest.fixture
async def business(db):
    biz = await _make_business(db)
    yield biz
    await _cleanup(db, biz)


async def _make_receipt(db, order, business) -> DigitalReceipt:
    customer = Customer(business_id=business.id, wa_id="27821234567")
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    with patch("backend.app.bot.whatsapp_sender.send_template_message", AsyncMock(return_value=("wamid.X", None))):
        receipt = await send_digital_receipt(db, order, business, channels={"WHATSAPP"}, customer=customer)
    return receipt


class TestValidTokenRendersCorrectData:
    async def test_renders_trading_name_order_number_and_total(self, db, business):
        order = await _make_order_with_item(db, business)
        receipt = await _make_receipt(db, order, business)

        response = await _HANDLER(receipt.token, None, db)
        assert response.status_code == 200
        html = response.body.decode("utf-8")
        assert business.name in html
        assert order.order_number in html
        assert "R240.00" in html
        assert "Large Chicken Pizza" in html
        assert response.headers["cache-control"] == "private, no-store"


class TestHistoricalIntegrity:
    async def test_menu_price_change_after_order_does_not_change_the_public_page(self, db, business):
        menu_item = MenuItem(business_id=business.id, name="Large Chicken Pizza", price_cents=24400)
        db.add(menu_item)
        await db.commit()
        await db.refresh(menu_item)

        order = await _make_order_with_item(db, business, menu_item=menu_item)
        receipt = await _make_receipt(db, order, business)

        before = await _HANDLER(receipt.token, None, db)
        assert "R240.00" in before.body.decode("utf-8")

        menu_item.price_cents = 999999
        business.vat_rate_basis_points = 2000
        await db.commit()

        after = await _HANDLER(receipt.token, None, db)
        after_html = after.body.decode("utf-8")
        assert "R240.00" in after_html
        assert "9999.99" not in after_html
        assert "9,999.99" not in after_html


class TestInvalidTokenReturnsGeneric404:
    async def test_unknown_token(self, db):
        response = await _HANDLER("this-token-does-not-exist", None, db)
        assert response.status_code == 404
        assert "Receipt not found" in response.body.decode("utf-8")
        assert response.headers["cache-control"] == "private, no-store"

    async def test_malformed_token(self, db):
        response = await _HANDLER("<script>alert(1)</script>", None, db)
        assert response.status_code == 404

    async def test_empty_token_like_input(self, db):
        response = await _HANDLER("   ", None, db)
        assert response.status_code == 404


class Test404ShapeIndistinguishable:
    async def test_unknown_token_and_a_second_unknown_token_return_identical_shape(self, db, business):
        order = await _make_order_with_item(db, business)
        receipt = await _make_receipt(db, order, business)
        wrong_token = receipt.token[:-1] + ("A" if receipt.token[-1] != "A" else "B")

        r1 = await _HANDLER("totally-made-up-token", None, db)
        r2 = await _HANDLER(wrong_token, None, db)
        assert r1.status_code == r2.status_code == 404
        assert r1.body == r2.body
        assert r1.media_type == r2.media_type


class TestNoLookupFromUrlOrQuery:
    def test_handler_signature_only_accepts_token(self):
        import inspect
        sig = inspect.signature(_HANDLER)
        param_names = set(sig.parameters.keys())
        # request/db are framework plumbing, never client-suppliable
        # business/order/customer ids — confirm none of those appear.
        assert "business_id" not in param_names
        assert "order_id" not in param_names
        assert "customer_id" not in param_names
        assert "token" in param_names

    async def test_a_second_businesss_order_id_is_never_reachable_via_this_token(self, db, business):
        """Even conceptually attempting to pass another business's order_id
        has no code path here at all — the handler never accepts one. This
        proves the ONLY key is the token by showing two receipts (two
        tokens) resolve to their own, and only their own, order."""
        other = await _make_business(db)
        try:
            order_a = await _make_order_with_item(db, business)
            order_b = await _make_order_with_item(db, other)
            receipt_a = await _make_receipt(db, order_a, business)
            receipt_b = await _make_receipt(db, order_b, other)

            resp_a = await _HANDLER(receipt_a.token, None, db)
            resp_b = await _HANDLER(receipt_b.token, None, db)
            html_a = resp_a.body.decode("utf-8")
            html_b = resp_b.body.decode("utf-8")
            assert order_a.order_number in html_a
            assert order_b.order_number not in html_a
            assert order_b.order_number in html_b
            assert order_a.order_number not in html_b
        finally:
            await _cleanup(db, other)


class TestDisablingEntitlementNeverAffectsPublicPage:
    async def test_receipt_still_viewable_after_entitlement_disabled(self, db, business):
        order = await _make_order_with_item(db, business)
        receipt = await _make_receipt(db, order, business)

        business.digital_receipts_enabled = False
        await db.commit()

        response = await _HANDLER(receipt.token, None, db)
        assert response.status_code == 200
        assert order.order_number in response.body.decode("utf-8")


class TestRateLimitConfiguration:
    """
    Live engagement of the per-IP rate limit needs a reachable Redis
    (slowapi's storage backend) — unavailable in this sandbox (verified:
    connection to localhost:6379 refused). This test instead verifies,
    WITHOUT a live backend, that the decorator is actually configured with
    the intended limit — the same static-configuration check pattern as
    every other route with @limiter.limit in this codebase relies on for
    the string being correct. Live engagement is Unknown in this
    environment and called out as such in the task report.
    """
    def test_public_receipt_route_has_the_configured_rate_limit(self):
        name = f"{_HANDLER.__module__}.{_HANDLER.__name__}"
        configured = limiter._route_limits.get(name, [])
        assert configured, f"no rate limit configured for {name}"
        limit_strings = [str(item.limit) for item in configured]
        assert any(str(settings.RECEIPT_PUBLIC_RATE_LIMIT_PER_MINUTE) in s for s in limit_strings)
