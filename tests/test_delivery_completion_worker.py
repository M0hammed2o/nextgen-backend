"""
Marketplace completion pass (2026-10-26) — worker-plane behaviour added on
top of `test_delivery_order_service.py`, using the same TypedFakeDB doubles:

* modifier/option/removal resolution parity with the CSV importer, and the
  unresolved-entry contract the mapping screen's deep link reads;
* item 86ing (MenuItem/MenuAddOn.is_active=False) -> ITEM_AVAILABILITY deny;
* never booking a sale already recorded (cross-path dedupe);
* sandbox test tenants (allow-list) vs real tenants;
* kitchen -> provider status push (ready / restaurant cancel / hand-over);
* scheduled-order re-accept and customer-edit handling.

Real-Postgres proofs of exactly-once/idempotency live in
tests/integration/test_delivery_marketplace_realdb.py.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.delivery import order_service
from backend.app.delivery.canonical import (
    CanonicalDeliveryLine,
    CanonicalDeliveryModifier,
    CanonicalDeliveryOrder,
)
from backend.app.delivery.entitlements import is_sandbox_test_connection, may_process_orders
from backend.app.delivery.errors import DeliveryCapabilityUnsupported, DeliveryProviderError
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order, OrderItem
from tests.test_delivery_order_service import (
    ScalarResult,
    TypedFakeDB,
    _business,
    _connection,
    _delivery_order,
)

SIZE_OPTIONS = {
    "option_groups": [{
        "id": "grp-size", "name": "Size",
        "options": [
            {"id": "opt-small", "name": "Small", "price_delta_cents": 0},
            {"id": "opt-large", "name": "Large", "price_delta_cents": 2000},
            {"id": "opt-family", "name": "Family", "price_delta_cents": 4000, "is_enabled": False},
        ],
    }],
    "removable_ingredients": [{"id": "rm-onion", "name": "Onion"}],
}


def _item(business_id, name="Margherita", **overrides) -> MenuItem:
    item = MenuItem(
        id=uuid.uuid4(), business_id=business_id, name=name, is_deleted=False,
        is_active=True, price_cents=9000, options_json=SIZE_OPTIONS,
    )
    for k, v in overrides.items():
        setattr(item, k, v)
    return item


def _mapping(business_id, kind, external_id, **targets) -> ExternalProductMapping:
    return ExternalProductMapping(
        id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind=kind,
        external_text=external_id, external_key=external_id.lower(), external_id=external_id,
        status="MAPPED", is_ignored=False, **targets,
    )


def _order(lines, **overrides) -> CanonicalDeliveryOrder:
    canonical = CanonicalDeliveryOrder(
        provider="UBER_EATS", external_order_id="order-1", external_store_id="store-1",
        lines=lines, placed_at=datetime.now(timezone.utc), display_id="AB123",
        subtotal_cents=11000, tax_cents=0, fee_cents=500, total_cents=11500,
    )
    for k, v in overrides.items():
        setattr(canonical, k, v)
    return canonical


async def _resolve(business_id, canonical, mappings=(), items=(), add_ons=()):
    db = TypedFakeDB()
    db.register_select(ExternalProductMapping, ScalarResult(rows=list(mappings)))
    db.register_select(MenuItem, ScalarResult(rows=list(items)))
    db.register_select(MenuAddOn, ScalarResult(rows=list(add_ons)))
    return await order_service._resolve_delivery_lines(
        db, business_id=business_id, provider="UBER_EATS", canonical=canonical,
    )


# ── Resolution parity with the CSV importer ──────────────────────────────────

class TestModifierResolution:
    async def test_modifier_mapped_to_an_option_resolves_instead_of_blocking(self):
        """Previously only add-on targets resolved; an option mapping left NEEDS_MAPPING forever."""
        business_id = uuid.uuid4()
        item = _item(business_id)
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-large", title="Lrg", group_title="Size"),),
        )])
        result = await _resolve(
            business_id, canonical,
            mappings=[_mapping(business_id, "MODIFIER", "u-large", option_id="opt-large")],
            items=[item],
        )
        assert result.blocks_import is False
        assert result.lines[0].options[0]["option_id"] == "opt-large"
        assert result.lines[0].options[0]["group_id"] == "grp-size"

    async def test_option_matched_by_exact_name_on_the_item(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-sm", title="small"),),
        )])
        result = await _resolve(business_id, canonical, items=[_item(business_id)])
        assert result.blocks_import is False
        assert result.lines[0].options[0]["option_id"] == "opt-small"

    async def test_removed_default_ingredient_resolves_to_the_removable_ref(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-onion", title="Onion", removed=True),),
        )])
        result = await _resolve(business_id, canonical, items=[_item(business_id)])
        assert result.blocks_import is False
        assert result.lines[0].removed == [{"id": "rm-onion", "name": "Onion"}]

    async def test_removal_mapping_by_ref_id(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-noonion", title="Hold the onions"),),
        )])
        result = await _resolve(
            business_id, canonical,
            mappings=[_mapping(business_id, "MODIFIER", "u-noonion", removable_ref_id="rm-onion")],
            items=[_item(business_id)],
        )
        assert result.blocks_import is False
        assert result.lines[0].removed[0]["id"] == "rm-onion"

    async def test_unresolved_modifier_entry_carries_what_the_mapping_screen_needs(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-anch", title="Anchovies", group_title="Extras"),),
        )])
        result = await _resolve(business_id, canonical, items=[_item(business_id)])
        assert result.blocks_import is True
        assert result.unresolved_entries == [{
            "kind": "MODIFIER", "external_id": "u-anch", "external_text": "Anchovies",
            "parent_external_text": "Margherita", "group_title": "Extras", "removed": False,
        }]


class TestAvailability:
    async def test_inactive_menu_item_is_reported_unavailable(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(external_item_id="u-marg", title="Margherita", quantity=1)])
        result = await _resolve(business_id, canonical, items=[_item(business_id, is_active=False)])
        assert result.blocks_import is False
        assert result.unavailable == [{"external_id": "u-marg", "external_text": "Margherita"}]

    async def test_disabled_option_is_reported_unavailable(self):
        business_id = uuid.uuid4()
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-fam", title="Family"),),
        )])
        result = await _resolve(business_id, canonical, items=[_item(business_id)])
        assert result.unavailable == [{"external_id": "u-fam", "external_text": "Family"}]

    async def test_inactive_add_on_is_reported_unavailable(self):
        business_id = uuid.uuid4()
        add_on = MenuAddOn(id=uuid.uuid4(), business_id=business_id, name="Extra Cheese",
                           is_deleted=False, is_active=False, price_cents=1500)
        canonical = _order([CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            modifiers=(CanonicalDeliveryModifier(external_id="u-ch", title="Extra Cheese"),),
        )])
        result = await _resolve(business_id, canonical, items=[_item(business_id)], add_ons=[add_on])
        assert result.unavailable == [{"external_id": "u-ch", "external_text": "Extra Cheese"}]

    async def test_order_for_an_86d_item_is_denied_with_item_availability(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        row = _delivery_order(business_id, connection.id)
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, _business())
        db.register_select(Order, ScalarResult(rows=[]))
        db.register_select(ExternalProductMapping, ScalarResult(rows=[]))
        db.register_select(MenuItem, ScalarResult(rows=[_item(business_id, is_active=False)]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        adapter = MagicMock()
        adapter.fetch_order = AsyncMock(return_value=_order([
            CanonicalDeliveryLine(external_item_id="u-marg", title="Margherita", quantity=1, unit_price_cents=11000),
        ]))
        adapter.deny_order = AsyncMock()
        adapter.accept_order = AsyncMock()
        with patch.object(order_service.registry, "get_provider", return_value=adapter), \
             patch.object(order_service, "_publish_alert", AsyncMock()):
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1", event_type=None,
            )
        _, kwargs = adapter.deny_order.call_args
        assert kwargs["reason_code"] == "ITEM_AVAILABILITY"
        assert kwargs["out_of_stock_items"] == ["u-marg"]
        assert row.status == "DENIED"
        adapter.accept_order.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added)


# ── Order creation ───────────────────────────────────────────────────────────

class TestOrderCreation:
    async def _create(self, canonical, resolution_lines, *, taken_numbers=()):
        business = _business(inventory_enabled=False, vat_registered=False)
        db = TypedFakeDB()
        db.register_select(Order, ScalarResult(rows=[
            Order(order_number=n) for n in taken_numbers
        ]))
        resolution = order_service._ResolutionResult(lines=resolution_lines)
        order = await order_service._create_order_from_canonical(
            db, business=business, provider="UBER_EATS", canonical=canonical, resolution=resolution,
        )
        return order, [o for o in db.added if isinstance(o, OrderItem)]

    async def test_books_ubers_documented_money_fields_not_total_as_food(self):
        line = CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            unit_price_cents=11000, total_price_cents=11000, base_unit_price_cents=9000,
        )
        resolved = order_service._ResolvedDeliveryLine(canonical=line, menu_item_id=uuid.uuid4(), menu_item_name="Margherita")
        order, items = await self._create(_order([line]), [resolved])
        assert order.subtotal_cents == 11000, "sub_total: the food sale"
        assert order.delivery_fee_cents == 500, "total_fee: fees paid out to the merchant"
        assert order.total_cents == 11500, "total: what the merchant is paid, as Uber reports it"
        assert (items[0].unit_price_cents, items[0].line_total_cents, items[0].base_price_cents) == (11000, 11000, 9000)

    async def test_add_on_snapshot_carries_name_and_channel_price(self):
        add_on = MenuAddOn(id=uuid.uuid4(), name="Extra Cheese", price_cents=1500)
        modifier = CanonicalDeliveryModifier(external_id="u-ch", title="Cheese+", quantity=2, price_cents=1000)
        line = CanonicalDeliveryLine(
            external_item_id="u-marg", title="Margherita", quantity=1,
            unit_price_cents=11000, total_price_cents=11000, base_unit_price_cents=9000, modifiers=(modifier,),
        )
        resolved = order_service._ResolvedDeliveryLine(
            canonical=line, menu_item_id=uuid.uuid4(), menu_item_name="Margherita", add_ons=[(add_on, modifier)],
        )
        _, items = await self._create(_order([line]), [resolved])
        assert items[0].add_ons_snapshot == [
            {"add_on_id": str(add_on.id), "name": "Extra Cheese", "quantity": 2, "price_cents": 1000},
        ]
        assert items[0].add_on_total_cents == 2000

    async def test_order_number_uses_the_display_code_and_survives_reuse(self):
        line = CanonicalDeliveryLine(external_item_id="x", title="Margherita", quantity=1, unit_price_cents=11000)
        resolved = order_service._ResolvedDeliveryLine(canonical=line, menu_item_id=uuid.uuid4())
        fresh, _ = await self._create(_order([line]), [resolved])
        assert fresh.order_number == "UE-AB123"
        reused, _ = await self._create(_order([line]), [resolved], taken_numbers=["UE-AB123"])
        assert reused.order_number == "UE-AB123-orde"

    async def test_pickup_order_mode_and_phone_code_note(self):
        line = CanonicalDeliveryLine(external_item_id="x", title="Margherita", quantity=1, unit_price_cents=11000)
        resolved = order_service._ResolvedDeliveryLine(canonical=line, menu_item_id=uuid.uuid4())
        order, _ = await self._create(
            _order([line], order_type="PICK_UP", customer_phone="+27 21 555 0100", customer_phone_code="123 45"),
            [resolved],
        )
        assert order.order_mode == "PICKUP"
        assert order.phone_number == "+27 21 555 0100"
        assert "phone code: 123 45" in order.special_instructions


class TestNeverBookedTwice:
    async def test_existing_sale_for_this_order_is_not_booked_again_and_provider_is_not_called(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        row = _delivery_order(business_id, connection.id)
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, _business())
        db.register_select(Order, ScalarResult(rows=[Order(id=uuid.uuid4(), order_number="UE-AB123")]))
        adapter = MagicMock()
        adapter.fetch_order = AsyncMock(return_value=_order([
            CanonicalDeliveryLine(external_item_id="u-marg", title="Margherita", quantity=1, unit_price_cents=11000),
        ]))
        adapter.accept_order = AsyncMock()
        adapter.deny_order = AsyncMock()
        with patch.object(order_service.registry, "get_provider", return_value=adapter), \
             patch.object(order_service, "_publish_alert", AsyncMock()):
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1", event_type=None,
            )
        assert row.status == "FAILED"
        assert "UE-AB123" in row.failure_reason
        adapter.accept_order.assert_not_awaited()
        adapter.deny_order.assert_not_awaited()


# ── Sandbox test tenants ─────────────────────────────────────────────────────

class TestSandboxTestTenants:
    def _sandbox(self, business_id):
        return _connection(business_id, status="SANDBOX", environment="sandbox")

    def test_allow_listed_sandbox_tenant_may_process_but_is_never_live(self):
        business_id = uuid.uuid4()
        connection = self._sandbox(business_id)
        from backend.app.core.config import get_settings
        with patch.object(get_settings(), "UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS", f"junk, {business_id}"):
            assert is_sandbox_test_connection(connection) is True
            assert may_process_orders(connection) is True
            assert order_service._find_misconfiguration(
                connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
            ) is None

    def test_sandbox_connection_on_a_real_tenant_is_still_denied(self):
        connection = self._sandbox(uuid.uuid4())
        from backend.app.core.config import get_settings
        with patch.object(get_settings(), "UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS", ""):
            assert may_process_orders(connection) is False
            assert order_service._find_misconfiguration(
                connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
            ) == "CONNECTION_NOT_LIVE"

    def test_allow_list_never_unlocks_a_production_environment_connection(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, status="SANDBOX", environment="production")
        from backend.app.core.config import get_settings
        with patch.object(get_settings(), "UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS", str(business_id)):
            assert may_process_orders(connection) is False

    async def test_env_mismatch_is_denied_through_the_environment_the_order_came_from(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, environment="sandbox", status="SANDBOX")
        event = DeliveryWebhookEvent(id=uuid.uuid4(), environment="production")
        row = _delivery_order(business_id, connection.id, raw_event_id=event.id)
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, _business())
        db.register_get(DeliveryWebhookEvent, event)
        adapter = MagicMock()
        adapter.deny_order = AsyncMock()
        with patch.object(order_service.registry, "get_provider", return_value=adapter), \
             patch.object(order_service, "_publish_alert", AsyncMock()):
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1", event_type=None,
            )
        assert row.failure_reason == "ENV_MISMATCH"
        assert adapter.deny_order.call_args.kwargs["context"].environment == "production"


# ── Kitchen -> provider status push ─────────────────────────────────────────

class TestStatusSync:
    async def _sync(self, row, new_status, *, adapter, reason=None):
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, _connection(row.business_id))
        with patch.object(order_service.registry, "get_provider", return_value=adapter), \
             patch.object(order_service, "_publish_alert", AsyncMock()) as alert:
            await order_service._sync_order_status(
                db, business_id=row.business_id, order_id=uuid.uuid4(), new_status=new_status, reason=reason,
            )
        return db, alert

    async def test_ready_is_pushed_and_recorded(self):
        row = _delivery_order(uuid.uuid4(), uuid.uuid4(), status="ACCEPTED")
        adapter = MagicMock(mark_order_ready=AsyncMock())
        db, alert = await self._sync(row, "READY", adapter=adapter)
        adapter.mark_order_ready.assert_awaited_once()
        assert row.last_provider_sync_at is not None and row.last_provider_sync_error is None
        assert any(isinstance(a, AuditEvent) and a.action == "delivery.order.ready_signalled" for a in db.added)
        alert.assert_not_awaited()

    async def test_failed_ready_push_is_recorded_and_raised_never_reversed(self):
        row = _delivery_order(uuid.uuid4(), uuid.uuid4(), status="ACCEPTED")
        adapter = MagicMock(mark_order_ready=AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "x", 500)))
        _, alert = await self._sync(row, "READY", adapter=adapter)
        assert row.status == "ACCEPTED"
        assert row.last_provider_sync_error.startswith("Could not mark ready")
        alert.assert_awaited_once()

    async def test_unsupported_provider_capability_tells_staff_where_to_do_it(self):
        row = _delivery_order(uuid.uuid4(), uuid.uuid4(), status="ACCEPTED")
        adapter = MagicMock(mark_order_ready=AsyncMock(side_effect=DeliveryCapabilityUnsupported("UBER_EATS", "ready_signal")))
        await self._sync(row, "READY", adapter=adapter)
        assert "tablet" in row.last_provider_sync_error

    async def test_staff_cancel_is_pushed_as_a_restaurant_cancel(self):
        row = _delivery_order(uuid.uuid4(), uuid.uuid4(), status="ACCEPTED")
        adapter = MagicMock(cancel_order=AsyncMock())
        await self._sync(row, "CANCELLED", adapter=adapter, reason="Out of dough")
        kwargs = adapter.cancel_order.call_args.kwargs
        assert kwargs["reason_code"] == "OTHER" and kwargs["explanation"] == "Out of dough"
        assert row.status == "CANCELLED" and row.cancelled_at is not None

    async def test_hand_over_closes_the_row_without_any_provider_call(self):
        row = _delivery_order(uuid.uuid4(), uuid.uuid4(), status="ACCEPTED")
        adapter = MagicMock()
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        with patch.object(order_service.registry, "get_provider", return_value=adapter):
            await order_service._sync_order_status(
                db, business_id=row.business_id, order_id=uuid.uuid4(), new_status="COLLECTED", reason=None,
            )
        assert row.status == "FULFILLED"
        assert adapter.method_calls == []

    async def test_non_marketplace_order_is_a_no_op(self):
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=None))
        await order_service._sync_order_status(
            db, business_id=uuid.uuid4(), order_id=uuid.uuid4(), new_status="READY", reason=None,
        )
        assert db.commits == 0


# ── Scheduled orders / customer edits ────────────────────────────────────────

class TestScheduledAndEdits:
    async def test_scheduled_order_is_accepted_again_on_its_live_notification_without_a_second_sale(self):
        business_id = uuid.uuid4()
        event = DeliveryWebhookEvent(id=uuid.uuid4(), event_type="orders.scheduled.notification")
        row = _delivery_order(business_id, uuid.uuid4(), status="ACCEPTED", raw_event_id=event.id, order_id=uuid.uuid4())
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryWebhookEvent, event)
        db.register_get(DeliveryConnection, _connection(business_id))
        adapter = MagicMock(accept_order=AsyncMock())
        with patch.object(order_service.registry, "get_provider", return_value=adapter):
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1",
                event_type="orders.notification",
            )
        adapter.accept_order.assert_awaited_once()
        assert not any(isinstance(o, Order) for o in db.added)
        assert any(isinstance(a, AuditEvent) and a.action == "delivery.order.scheduled_reaccepted" for a in db.added)

    async def test_ordinary_duplicate_notification_for_an_accepted_order_stays_a_no_op(self):
        business_id = uuid.uuid4()
        event = DeliveryWebhookEvent(id=uuid.uuid4(), event_type="orders.notification")
        row = _delivery_order(business_id, uuid.uuid4(), status="ACCEPTED", raw_event_id=event.id)
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryWebhookEvent, event)
        adapter = MagicMock(accept_order=AsyncMock())
        with patch.object(order_service.registry, "get_provider", return_value=adapter):
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1",
                event_type="orders.notification",
            )
        adapter.accept_order.assert_not_awaited()
        assert db.commits == 0

    async def test_edit_after_acceptance_flags_the_order_for_a_human(self):
        business_id = uuid.uuid4()
        row = _delivery_order(business_id, uuid.uuid4(), status="ACCEPTED")
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        with patch.object(order_service, "_publish_alert", AsyncMock()) as alert:
            await order_service._handle_order(
                db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1",
                event_type="orders.customer_order_edit",
            )
        assert row.status == "ACCEPTED"
        assert "edited" in row.last_provider_sync_error
        alert.assert_awaited_once()

    async def test_edit_before_acceptance_refetches_the_order(self):
        business_id = uuid.uuid4()
        row = _delivery_order(business_id, uuid.uuid4(), status="READY_TO_ACCEPT", unresolved_items_json=[])
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        # The recursive re-fetch is stubbed so this test stays on the edit path.
        with patch.object(order_service, "_handle_order", new=AsyncMock()) as handle:
            await order_service._handle_order_edit(db, row=row)
        assert row.status == "RECEIVED"
        assert row.unresolved_items_json is None, "the stale snapshot's verdict is discarded"
        handle.assert_awaited_once()
        assert handle.call_args.kwargs["event_type"] is None


def test_watchdog_scan_list_never_includes_an_unconfirmed_accept():
    assert "ACCEPT_UNCONFIRMED" not in order_service.OPEN_STATUSES
    assert set(order_service.ACCEPTED_STATUSES) == {"ACCEPTED", "ACCEPT_UNCONFIRMED"}
