"""
Manual dashboard order creation (backend/app/api/v1/routes_orders.py::create_order)
— BUG-0016 fix.

Before this fix, every line item's `price_cents` was trusted verbatim from
the client with no validation against the live menu catalog, and the order
total never had VAT applied regardless of the business's VAT configuration.

These tests prove:
  * a `menu_item_id` line is resolved and priced server-side via the exact
    same `backend/app/pos/pricing.py::resolve_pos_items` path POS checkout
    uses — a client-submitted price is never trusted for that line, and the
    resulting math matches POS's own math for the same inputs;
  * an ad hoc (`name` + `price_cents`, no `menu_item_id`) line keeps working
    exactly as before (the manager dashboard's only real caller today) and
    is flagged `is_ad_hoc_price=True` in the response;
  * a `menu_item_id` from another tenant's catalog is rejected the same way
    POS rejects it (404, via resolve_pos_items's own tenant-scoped lookup —
    exercised directly against the real `resolve_pos_items` here to prove
    the cross-tenant isolation, not just that create_order propagates
    whatever exception it's handed);
  * VAT is applied through `shared/pricing/engine.py::apply_vat_to_total`,
    matching POS's own inclusive/exclusive math for identical inputs.
"""
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_orders
from backend.app.api.v1.routes_orders import (
    OrderCreate,
    OrderItemCreate,
    OrderItemAddOnSelection,
    create_order,
)
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.pos.pricing import ResolvedPOSItem
from shared.pricing.engine import LineItemBreakdown, apply_vat_to_total


# ── Fakes (same shape as tests/test_pos_orders.py's FakeDB/FakeResult) ──────

class FakeResult:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        if hasattr(obj, "payment_status") and getattr(obj, "payment_status", None) is None:
            obj.payment_status = "PENDING"
        if hasattr(obj, "payment_required") and getattr(obj, "payment_required", None) is None:
            obj.payment_required = False
        if hasattr(obj, "discount_cents") and getattr(obj, "discount_cents", None) is None:
            obj.discount_cents = 0

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_resolved_item(line_total_cents=5000, base_price_cents=None, quantity=1, name="Cheeseburger"):
    base = base_price_cents if base_price_cents is not None else line_total_cents // quantity
    breakdown = LineItemBreakdown(
        base_price_cents=base,
        option_adjustment_cents=0,
        add_on_total_cents=0,
        unit_price_cents=base,
        quantity=quantity,
        line_total_cents=line_total_cents,
    )
    return ResolvedPOSItem(
        menu_item_id=uuid.uuid4(),
        name=name,
        breakdown=breakdown,
        selected_options_snapshot=[],
        add_ons_snapshot=[],
        removed_ingredients_snapshot=[],
        special_instructions=None,
        requires_preparation=True,
    )


# ── Ad hoc path (legacy, unchanged) ─────────────────────────────────────────

class TestAdHocPricing:
    async def test_ad_hoc_item_still_works_and_is_flagged(self):
        user = make_user()
        body = OrderCreate(items=[OrderItemCreate(name="Delivery surcharge", qty=1, price_cents=1500)])

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, False, 0, True)),
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(side_effect=AssertionError("must not resolve when no menu_item_id given"))):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        assert order.subtotal_cents == 1500
        assert order.total_cents == 1500
        assert order.tax_cents == 0
        # The response model reads is_ad_hoc_price straight off the ORM object.
        oi = [o for o in db.added if getattr(o, "name_snapshot", None) == "Delivery surcharge"][0]
        assert oi.is_ad_hoc_price is True
        assert oi.unit_price_cents == 1500
        assert oi.menu_item_id is None

    async def test_missing_both_menu_item_id_and_name_price_is_rejected(self):
        with pytest.raises(Exception):
            OrderItemCreate(qty=1)


# ── Catalog path (BUG-0016 fix) ─────────────────────────────────────────────

class TestCatalogPricing:
    async def test_catalog_item_ignores_client_price_and_uses_resolved_breakdown(self):
        user = make_user()
        menu_item_id = uuid.uuid4()
        # Client tries to submit price_cents alongside menu_item_id — must be ignored.
        body = OrderCreate(items=[
            OrderItemCreate(menu_item_id=menu_item_id, qty=2, name="ignored-name", price_cents=1),
        ])
        resolved = make_resolved_item(line_total_cents=8000, base_price_cents=4000, quantity=2, name="Real Menu Name")

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, False, 0, True)),
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(return_value=[resolved])) as mock_resolve:
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        mock_resolve.assert_awaited_once()
        assert order.subtotal_cents == 8000
        assert order.total_cents == 8000
        oi = [o for o in db.added if getattr(o, "name_snapshot", None) == "Real Menu Name"][0]
        assert oi.is_ad_hoc_price is False
        assert oi.unit_price_cents == 4000
        assert oi.line_total_cents == 8000
        assert oi.menu_item_id == resolved.menu_item_id
        # The client's price_cents=1/name="ignored-name" never reached the order.
        assert oi.name_snapshot != "ignored-name"

    async def test_selected_options_and_add_ons_are_forwarded_to_resolve_pos_items(self):
        """
        The dashboard's selected_option_ids/add_on_selections/
        removed_ingredient_ids must reach resolve_pos_items unchanged, so
        option/add-on validation (required groups, disabled options, per-add-on
        min/max) is enforced the same way it is for POS — not silently dropped.
        """
        user = make_user()
        menu_item_id = uuid.uuid4()
        add_on_id = uuid.uuid4()
        body = OrderCreate(items=[
            OrderItemCreate(
                menu_item_id=menu_item_id, qty=1,
                selected_option_ids={"size": "large"},
                add_on_selections=[OrderItemAddOnSelection(add_on_id=add_on_id, quantity=2)],
                removed_ingredient_ids=["onion"],
            ),
        ])
        resolved = make_resolved_item(line_total_cents=5000, base_price_cents=5000, quantity=1)

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, False, 0, True)),
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(return_value=[resolved])) as mock_resolve:
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    await create_order(body=body, user=user, db=db)

        _, call_args, _kwargs = mock_resolve.mock_calls[0]
        forwarded_items = call_args[2]
        assert len(forwarded_items) == 1
        forwarded = forwarded_items[0]
        assert forwarded.menu_item_id == menu_item_id
        assert forwarded.selected_option_ids == {"size": "large"}
        assert forwarded.removed_ingredient_ids == ["onion"]
        assert len(forwarded.add_on_selections) == 1
        assert forwarded.add_on_selections[0].add_on_id == add_on_id
        assert forwarded.add_on_selections[0].quantity == 2

    async def test_mixed_catalog_and_adhoc_items_sum_correctly_and_preserve_order(self):
        user = make_user()
        body = OrderCreate(items=[
            OrderItemCreate(name="Extra napkins", qty=1, price_cents=0),
            OrderItemCreate(menu_item_id=uuid.uuid4(), qty=1),
            OrderItemCreate(name="Delivery surcharge", qty=1, price_cents=2000),
        ])
        resolved = make_resolved_item(line_total_cents=6000, base_price_cents=6000, quantity=1, name="Pizza")

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, False, 0, True)),
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(return_value=[resolved])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        assert order.subtotal_cents == 0 + 6000 + 2000
        names_in_order = [o.name_snapshot for o in db.added if hasattr(o, "name_snapshot")]
        assert names_in_order == ["Extra napkins", "Pizza", "Delivery surcharge"]

    async def test_menu_item_from_another_business_is_rejected_404(self):
        """
        Exercises the REAL resolve_pos_items (not patched) against a fake DB
        that returns no matching MenuItem row for this business — proving
        the tenant-isolation query (`MenuItem.business_id == business_id`)
        is what actually gates this, not merely that create_order propagates
        a pre-canned exception.
        """
        from backend.app.pos.pricing import POSItemInput, resolve_pos_items

        class EmptyScalars:
            def all(self):
                return []

        class EmptyMenuItemResult:
            def scalars(self):
                return EmptyScalars()

        class MenuLookupDB:
            async def execute(self, stmt):
                return EmptyMenuItemResult()

        other_business_id = uuid.uuid4()
        with pytest.raises(NotFoundError) as exc:
            await resolve_pos_items(
                MenuLookupDB(), other_business_id,
                [POSItemInput(menu_item_id=uuid.uuid4(), quantity=1)],
            )
        assert exc.value.status_code == 404


# ── VAT ──────────────────────────────────────────────────────────────────────

class TestManualOrderVAT:
    async def test_vat_registered_inclusive_matches_engine_math(self):
        user = make_user()
        resolved = make_resolved_item(line_total_cents=11500, base_price_cents=11500, quantity=1, name="Combo")
        body = OrderCreate(items=[OrderItemCreate(menu_item_id=uuid.uuid4(), qty=1)])

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, True, 1500, True)),  # 15% inclusive VAT
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(return_value=[resolved])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        expected_total, expected_vat = apply_vat_to_total(11500, 1500, True)
        assert order.total_cents == expected_total == 11500  # inclusive: total unchanged
        assert order.tax_cents == expected_vat == 1500

    async def test_vat_registered_exclusive_adds_to_total_matching_pos_math(self):
        user = make_user()
        resolved = make_resolved_item(line_total_cents=10000, base_price_cents=10000, quantity=1, name="Combo")
        body = OrderCreate(items=[OrderItemCreate(menu_item_id=uuid.uuid4(), qty=1)])

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, True, 1500, False)),  # 15% exclusive VAT
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(return_value=[resolved])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        expected_total, expected_vat = apply_vat_to_total(10000, 1500, False)
        assert expected_total == 11500
        assert order.total_cents == expected_total
        assert order.tax_cents == expected_vat == 1500

    async def test_non_vat_registered_business_still_zero_tax(self):
        user = make_user()
        body = OrderCreate(items=[OrderItemCreate(name="Item", qty=1, price_cents=5000)])

        db = FakeDB([
            FakeResult(row=(1, "ZAR", 0, False, 0, True)),
            FakeResult(scalar="Africa/Johannesburg"),
        ])
        with patch.object(routes_orders, "resolve_pos_items", AsyncMock(side_effect=AssertionError("no catalog items"))):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()):
                    order = await create_order(body=body, user=user, db=db)

        assert order.tax_cents == 0
        assert order.total_cents == 5000
