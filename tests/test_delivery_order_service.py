"""
Worker plane (`backend/app/delivery/order_service.py`) — misconfiguration
gates + active deny, unmapped-item NEEDS_MAPPING (never a sale), stock
deduction exactly once, the no-inventory-entitlement path, cancellation,
provider timeout/retry, and the accept-deadline watchdog.

Uses a TypedFakeDB that dispatches `execute()` results by the SELECT's
target model class (robust to internal call reordering) — see module
docstring in test_delivery_intake.py for why this repo's test suite is
mocked rather than run against a live database in this session.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.delivery.canonical import (
    CanonicalDeliveryLine,
    CanonicalDeliveryModifier,
    CanonicalDeliveryOrder,
)
from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout
from backend.app.delivery.order_service import (
    _find_misconfiguration,
    _handle_cancellation,
    _handle_order,
    _resolve_delivery_lines,
    delivery_accept_deadline_watchdog,
    process_delivery_cancellation,
    process_delivery_order,
)
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder, DeliveryWebhookEvent
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order


# ── Test doubles ──────────────────────────────────────────────────────────────

class TypedFakeDB:
    """
    Dispatches `execute()` results by the target model class of a `select()`
    statement, and `get()` results by model class — order-independent,
    robust to the implementation reordering its own read calls.
    """

    def __init__(self):
        self._select_results: dict[type, list] = {}
        self._get_results: dict[type, list] = {}
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    def register_select(self, model: type, *results):
        self._select_results.setdefault(model, []).extend(results)

    def register_get(self, model: type, *results):
        self._get_results.setdefault(model, []).extend(results)

    async def execute(self, stmt):
        model = stmt.column_descriptions[0]["entity"]
        queue = self._select_results.get(model)
        assert queue, f"no scripted select() result registered for {model}"
        return queue.pop(0)

    async def get(self, model, id_):
        queue = self._get_results.get(model)
        if not queue:
            return None
        return queue.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class ScalarResult:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _ScalarsProxy(self._rows)


class _ScalarsProxy:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _business(**overrides) -> Business:
    b = Business(
        id=uuid.uuid4(), name="Test", slug="test", business_code="ABC123",
        is_active=True, delivery_platforms_enabled=True, uber_eats_enabled=True,
        inventory_enabled=False, vat_registered=False, currency="ZAR",
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def _connection(business_id, **overrides) -> DeliveryConnection:
    c = DeliveryConnection(
        id=uuid.uuid4(), business_id=business_id, provider="UBER_EATS",
        external_store_id="store-1", status="CONNECTED", environment="production",
        # Pre-existing tests in this file were written before migration 0046
        # (auto_accept_enabled) existed and assume the old zero-touch
        # behaviour — default it True here so they're unaffected; the
        # dedicated TestAutoAcceptGate class below explicitly exercises
        # auto_accept_enabled=False.
        auto_accept_enabled=True,
    )
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _delivery_order(business_id, connection_id, **overrides) -> DeliveryOrder:
    row = DeliveryOrder(
        id=uuid.uuid4(), business_id=business_id, provider="UBER_EATS",
        delivery_connection_id=connection_id, external_order_id="order-1",
        external_store_id="store-1", status="RECEIVED", raw_event_id=None,
        accept_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=600),
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _canonical_order(*, lines=None) -> CanonicalDeliveryOrder:
    return CanonicalDeliveryOrder(
        provider="UBER_EATS", external_order_id="order-1", external_store_id="store-1",
        lines=lines or [], placed_at=datetime.now(timezone.utc),
        total_cents=15000, subtotal_cents=15000, tax_cents=0, fee_cents=1000,
    )


def _menu_item(business_id, name="Large Margherita") -> MenuItem:
    return MenuItem(
        id=uuid.uuid4(), business_id=business_id, name=name, is_deleted=False,
        category_id=uuid.uuid4(), price_cents=15000,
    )


# ── _find_misconfiguration — pure, no DB ─────────────────────────────────────

class TestFindMisconfiguration:
    def test_missing_connection(self):
        assert _find_misconfiguration(
            connection=None, business=_business(), webhook_event=None, provider="UBER_EATS",
        ) == "CONNECTION_MISSING"

    def test_environment_mismatch(self):
        connection = _connection(uuid.uuid4(), environment="production")
        event = DeliveryWebhookEvent(environment="sandbox")
        assert _find_misconfiguration(
            connection=connection, business=_business(), webhook_event=event, provider="UBER_EATS",
        ) == "ENV_MISMATCH"

    def test_tenant_inactive(self):
        connection = _connection(uuid.uuid4())
        business = _business(is_active=False)
        assert _find_misconfiguration(
            connection=connection, business=business, webhook_event=None, provider="UBER_EATS",
        ) == "TENANT_INACTIVE"

    def test_entitlement_off_module_switch(self):
        connection = _connection(uuid.uuid4())
        business = _business(delivery_platforms_enabled=False)
        assert _find_misconfiguration(
            connection=connection, business=business, webhook_event=None, provider="UBER_EATS",
        ) == "ENTITLEMENT_OFF"

    def test_entitlement_off_provider_specific(self):
        connection = _connection(uuid.uuid4())
        business = _business(uber_eats_enabled=False)
        assert _find_misconfiguration(
            connection=connection, business=business, webhook_event=None, provider="UBER_EATS",
        ) == "ENTITLEMENT_OFF"

    def test_connection_not_live(self):
        connection = _connection(uuid.uuid4(), status="ATTENTION_REQUIRED")
        assert _find_misconfiguration(
            connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
        ) == "CONNECTION_NOT_LIVE"

    def test_sandbox_connection_is_not_treated_as_live(self):
        """
        Commercial-correctness requirement: Uber Eats production access
        needs Uber's own written approval, so a SANDBOX connection must
        never be allowed to process a real order, even though it is
        genuinely "connected" in a loose sense.
        """
        connection = _connection(uuid.uuid4(), status="SANDBOX")
        assert _find_misconfiguration(
            connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
        ) == "CONNECTION_NOT_LIVE"

    def test_awaiting_approval_connection_is_not_treated_as_live(self):
        """Same requirement as SANDBOX — waiting on Uber's own approval is not live."""
        connection = _connection(uuid.uuid4(), status="AWAITING_APPROVAL")
        assert _find_misconfiguration(
            connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
        ) == "CONNECTION_NOT_LIVE"

    def test_fully_configured_known_store_has_no_misconfiguration(self):
        connection = _connection(uuid.uuid4())
        assert _find_misconfiguration(
            connection=connection, business=_business(), webhook_event=None, provider="UBER_EATS",
        ) is None


# ── _resolve_delivery_lines — mapping resolution ─────────────────────────────

class TestResolveDeliveryLines:
    async def test_exact_external_id_mapping_resolves(self):
        business_id = uuid.uuid4()
        menu_item = _menu_item(business_id)
        mapping = ExternalProductMapping(
            id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
            external_text="Large Margherita", external_key="large margherita",
            external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
        )
        db = TypedFakeDB()
        db.register_select(ExternalProductMapping, ScalarResult(rows=[mapping]))
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title="Large Margherita", quantity=1)
        ])
        result = await _resolve_delivery_lines(
            db, business_id=business_id, provider="UBER_EATS", canonical=canonical
        )
        assert result.blocks_import is False
        assert result.lines[0].menu_item_id == menu_item.id

    async def test_unmapped_item_blocks_import_never_guesses(self):
        business_id = uuid.uuid4()
        db = TypedFakeDB()
        db.register_select(ExternalProductMapping, ScalarResult(rows=[]))
        db.register_select(MenuItem, ScalarResult(rows=[]))  # no exact-name fallback either
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-UNKNOWN", title="Mystery Pizza", quantity=1)
        ])
        result = await _resolve_delivery_lines(
            db, business_id=business_id, provider="UBER_EATS", canonical=canonical
        )
        assert result.blocks_import is True
        assert "Mystery Pizza" in result.unresolved_names

    async def test_exact_name_fallback_when_no_saved_mapping(self):
        business_id = uuid.uuid4()
        menu_item = _menu_item(business_id, name="Margherita")
        db = TypedFakeDB()
        db.register_select(ExternalProductMapping, ScalarResult(rows=[]))
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id=None, title="Margherita", quantity=1)
        ])
        result = await _resolve_delivery_lines(
            db, business_id=business_id, provider="UBER_EATS", canonical=canonical
        )
        assert result.lines[0].menu_item_id == menu_item.id

    async def test_unmapped_modifier_blocks_import_even_if_item_resolved(self):
        business_id = uuid.uuid4()
        menu_item = _menu_item(business_id)
        db = TypedFakeDB()
        db.register_select(ExternalProductMapping, ScalarResult(rows=[]))
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(
                external_item_id=None, title=menu_item.name, quantity=1,
                modifiers=(CanonicalDeliveryModifier(external_id="mod-X", title="Extra Anchovies"),),
            )
        ])
        result = await _resolve_delivery_lines(
            db, business_id=business_id, provider="UBER_EATS", canonical=canonical
        )
        assert result.blocks_import is True
        assert "Extra Anchovies" in result.unresolved_names

    async def test_mapped_modifier_resolves_to_add_on(self):
        business_id = uuid.uuid4()
        menu_item = _menu_item(business_id)
        add_on = MenuAddOn(id=uuid.uuid4(), business_id=business_id, name="Extra Cheese", is_deleted=False)
        mod_mapping = ExternalProductMapping(
            id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="MODIFIER",
            external_text="Extra Cheese", external_key="extra cheese",
            external_id="mod-cheese", menu_add_on_id=add_on.id, status="MAPPED",
        )
        db = TypedFakeDB()
        db.register_select(ExternalProductMapping, ScalarResult(rows=[mod_mapping]))
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[add_on]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(
                external_item_id=None, title=menu_item.name, quantity=1,
                modifiers=(CanonicalDeliveryModifier(external_id="mod-cheese", title="Extra Cheese"),),
            )
        ])
        result = await _resolve_delivery_lines(
            db, business_id=business_id, provider="UBER_EATS", canonical=canonical
        )
        assert result.blocks_import is False
        assert result.lines[0].add_on_ids == [add_on.id]


# ── _handle_order — misconfiguration -> active deny ─────────────────────────

class TestHandleOrderMisconfiguration:
    async def test_entitlement_off_known_store_gets_active_deny_not_silence(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(uber_eats_enabled=False)  # known store, entitlement off
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        # row.raw_event_id is None -> no DeliveryWebhookEvent get() call

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type="orders.notification",
            )

        fake_adapter.deny_order.assert_awaited_once()
        _, kwargs = fake_adapter.deny_order.call_args
        assert kwargs["reason_code"] == "STORE_CLOSED"
        assert row.status == "DENIED"
        assert row.deny_reason_text and "ENTITLEMENT_OFF" in row.failure_reason
        assert db.commits == 1

    async def test_connection_not_live_denies(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, status="ATTENTION_REQUIRED")
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock()
        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "DENIED"
        assert row.failure_reason == "CONNECTION_NOT_LIVE"

    async def test_deny_call_failure_still_marks_row_denied_locally(self):
        """A provider-communication failure on the deny call must never leave the row stuck."""
        business_id = uuid.uuid4()
        connection = _connection(business_id, status="NOT_CONNECTED")
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "boom"))
        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "DENIED"


# ── _handle_order — already in progress (idempotent no-op) ──────────────────

class TestHandleOrderAlreadyInProgress:
    async def test_non_received_status_is_a_no_op(self):
        business_id = uuid.uuid4()
        row = _delivery_order(business_id, uuid.uuid4(), status="ACCEPTED")
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        # No other selects registered — if the code tried to read further, this fails loudly.
        await _handle_order(
            db, provider="UBER_EATS", business_id=business_id,
            external_order_id="order-1", event_type=None,
        )
        assert db.commits == 0


# ── _handle_order — NEEDS_MAPPING: never auto-creates a sale ─────────────────

class TestHandleOrderNeedsMapping:
    async def test_unmapped_item_blocks_sale_creation(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(ExternalProductMapping, ScalarResult(rows=[]))
        db.register_select(MenuItem, ScalarResult(rows=[]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="unmapped-1", title="Mystery Item", quantity=1)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        assert row.status == "NEEDS_MAPPING"
        assert "Mystery Item" in row.unresolved_items_json
        fake_adapter.accept_order.assert_not_awaited()
        fake_adapter.deny_order.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added), "no Order must be created for an unmapped item"


# ── _handle_order — happy path: stock deducted exactly once ─────────────────

class TestHandleOrderHappyPath:
    async def test_fully_mapped_order_creates_sale_deducts_stock_once_and_accepts(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(inventory_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=2, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock(
                 return_value=MagicMock(lines=[1], menu_items_without_recipe=[])
             )) as mock_consumption, \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        assert row.status == "ACCEPTED"
        fake_adapter.accept_order.assert_awaited_once()
        mock_consumption.assert_awaited_once()  # exactly once
        created_orders = [o for o in db.added if isinstance(o, Order)]
        assert len(created_orders) == 1
        order = created_orders[0]
        assert order.till_session_id is None, "a delivery order must never reach till cash-up"
        assert order.delivery_address is None
        assert row.order_id == order.id

    async def test_no_inventory_entitlement_still_creates_sale_but_skips_stock(self):
        """business.inventory_enabled=False: the sale is still created, stock is not touched."""
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(inventory_enabled=False)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()) as mock_consumption, \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        mock_consumption.assert_not_awaited()
        created_orders = [o for o in db.added if isinstance(o, Order)]
        assert len(created_orders) == 1

    async def test_accept_call_failure_never_reverses_the_sale(self):
        """'The food was made' honesty — a failed accept call must not un-sell the order."""
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(inventory_enabled=False)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "network blip"))

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()), \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        created_orders = [o for o in db.added if isinstance(o, Order)]
        assert len(created_orders) == 1, "the sale must stand even though the accept call failed"
        assert row.order_id == created_orders[0].id
        assert db.commits == 1


# ── _handle_order — BUG-0091: a fetch failure must actively deny, never FAIL forever ──

class TestFetchErrorIsActivelyDenied:
    async def test_provider_error_on_fetch_issues_an_active_deny_not_a_dead_failed_state(self):
        """
        Previously a DeliveryProviderError (e.g. OperationalTokenUnavailable
        during a Redis outage) left the row status=FAILED with no deny ever
        issued, and FAILED is outside the watchdog's scanned status list —
        so it was stuck FOREVER. Must now behave like every other
        misconfiguration: an active deny, not silence.
        """
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(
            side_effect=DeliveryProviderError("UBER_EATS", "Redis unavailable — cannot mint token")
        )
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        fake_adapter.deny_order.assert_awaited_once()
        _, kwargs = fake_adapter.deny_order.call_args
        assert kwargs["reason_code"] == "POS_NOT_READY"
        assert row.status == "DENIED", "must never be left in a dead FAILED state"
        assert row.failure_reason and "fetch_order failed" in row.failure_reason
        assert db.commits == 1

    async def test_provider_error_on_fetch_still_denies_locally_even_if_the_deny_call_also_fails(self):
        """Same 'marked DENIED locally regardless' honesty as every other deny path."""
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "5xx"))
        fake_adapter.deny_order = AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "deny also failed"))

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "DENIED"


# ── _handle_order — BUG-0092: an unpriceable order must never book a sale ───

class TestUnpriceableOrderIsDenied:
    async def test_none_total_cents_denies_rather_than_booking_a_zero_value_sale(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(inventory_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        # Deliberately NO ExternalProductMapping/MenuItem/MenuAddOn registered
        # — if the code wrongly proceeds to _resolve_delivery_lines, this
        # test fails loudly rather than silently passing.

        canonical = CanonicalDeliveryOrder(
            provider="UBER_EATS", external_order_id="order-1", external_store_id="store-1",
            lines=[CanonicalDeliveryLine(
                external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500,
            )],
            placed_at=datetime.now(timezone.utc),
            total_cents=None, subtotal_cents=None, tax_cents=0, fee_cents=1000,
        )
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()) as mock_consumption:
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        fake_adapter.deny_order.assert_awaited_once()
        _, kwargs = fake_adapter.deny_order.call_args
        assert kwargs["reason_code"] == "PRICING"
        assert row.status == "DENIED"
        fake_adapter.accept_order.assert_not_awaited()
        mock_consumption.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added), "no sale may be booked for an unpriceable order"

    async def test_zero_total_cents_denies_rather_than_booking_a_zero_value_sale(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        canonical = _canonical_order(lines=[])
        canonical.total_cents = 0
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "DENIED"
        assert not any(isinstance(o, Order) for o in db.added)

    async def test_negative_total_cents_denies_rather_than_booking_a_negative_sale(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        canonical = _canonical_order(lines=[])
        canonical.total_cents = -500
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.deny_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "DENIED"
        assert not any(isinstance(o, Order) for o in db.added)

    async def test_positive_total_cents_still_creates_the_sale_normally(self):
        """Regression guard: the new pricing gate must not block a normal, well-priced order."""
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business(inventory_enabled=False)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()), \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )
        assert row.status == "ACCEPTED"
        created_orders = [o for o in db.added if isinstance(o, Order)]
        assert len(created_orders) == 1
        assert created_orders[0].total_cents == 15000


# ── _handle_order — migration 0046: auto_accept_enabled gate ────────────────

class TestAutoAcceptGate:
    async def test_auto_accept_disabled_stops_at_ready_to_accept_no_sale_created(self):
        """
        Design-gap fix: a fully-mapped order on a connection with
        auto_accept_enabled=False (the new default) must NOT auto-commit to
        the kitchen — it must wait for an explicit staff accept.
        """
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=False)
        business = _business(inventory_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()) as mock_consumption, \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()) as mock_alert:
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None,
            )

        assert row.status == "READY_TO_ACCEPT"
        assert row.order_id is None
        fake_adapter.accept_order.assert_not_awaited()
        mock_consumption.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added), "no sale until staff explicitly accepts"
        mock_alert.assert_awaited_once()
        assert mock_alert.call_args[0][1] == "delivery_ready_to_accept"

    async def test_force_accept_bypasses_the_gate_for_a_ready_to_accept_order(self):
        """
        The staff-confirm path: routes_delivery.py's manual accept endpoint
        re-enqueues with force_accept=True for a READY_TO_ACCEPT row — this
        must create the sale even though auto_accept_enabled is False.
        """
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=False)
        business = _business(inventory_enabled=False)
        row = _delivery_order(business_id, connection.id, status="RECEIVED")
        menu_item = _menu_item(business_id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)
        db.register_select(
            ExternalProductMapping,
            ScalarResult(rows=[ExternalProductMapping(
                id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
                external_text=menu_item.name, external_key=menu_item.name.lower(),
                external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
            )]),
        )
        db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
        db.register_select(MenuAddOn, ScalarResult(rows=[]))

        canonical = _canonical_order(lines=[
            CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1, unit_price_cents=7500)
        ])
        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(return_value=canonical)
        fake_adapter.accept_order = AsyncMock()

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter), \
             patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()), \
             patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()):
            await _handle_order(
                db, provider="UBER_EATS", business_id=business_id,
                external_order_id="order-1", event_type=None, force_accept=True,
            )

        assert row.status == "ACCEPTED"
        fake_adapter.accept_order.assert_awaited_once()
        created_orders = [o for o in db.added if isinstance(o, Order)]
        assert len(created_orders) == 1
        assert row.order_id == created_orders[0].id

    async def test_process_delivery_order_job_forwards_force_accept(self):
        session = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        with patch(
            "backend.app.delivery.order_service._handle_order", new=AsyncMock()
        ) as mock_handle:
            await process_delivery_order(
                ctx, "UBER_EATS", str(uuid.uuid4()), "order-1", None, True,
            )
        _, kwargs = mock_handle.call_args
        assert kwargs["force_accept"] is True


# ── _handle_order — provider timeout is retryable, not a permanent failure ──

class TestFetchTimeoutIsRetryable:
    async def test_timeout_reverts_row_to_received_and_re_raises(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        business = _business()
        row = _delivery_order(business_id, connection.id)

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, business)

        fake_adapter = MagicMock()
        fake_adapter.fetch_order = AsyncMock(side_effect=DeliveryProviderTimeout("timed out"))

        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            with pytest.raises(DeliveryProviderTimeout):
                await _handle_order(
                    db, provider="UBER_EATS", business_id=business_id,
                    external_order_id="order-1", event_type=None,
                )
        assert row.status == "RECEIVED"  # ready for a clean retry
        assert db.commits == 1

    async def test_process_delivery_order_job_lets_the_timeout_propagate_for_arq_retry(self):
        """ARQ's own max_tries must see the exception to actually retry the job."""
        session = AsyncMock()

        async def _raise(*a, **k):
            raise DeliveryProviderTimeout("timed out")

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        with patch("backend.app.delivery.order_service._handle_order", side_effect=_raise):
            with pytest.raises(DeliveryProviderTimeout):
                await process_delivery_order(ctx, "UBER_EATS", str(uuid.uuid4()), "order-1", None)
        session.rollback.assert_awaited_once()

    async def test_process_delivery_order_swallows_a_non_transient_failure(self):
        """A permanent/business-logic failure must not crash the worker process."""
        session = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        async def _raise(*a, **k):
            raise ValueError("some real bug")

        with patch("backend.app.delivery.order_service._handle_order", side_effect=_raise):
            await process_delivery_order(ctx, "UBER_EATS", str(uuid.uuid4()), "order-1", None)  # must not raise
        session.rollback.assert_awaited_once()


# ── Cancellation ──────────────────────────────────────────────────────────────

class TestCancellation:
    async def test_cancellation_marks_row_and_linked_order_cancelled_without_reversing_stock(self):
        business_id = uuid.uuid4()
        order_id = uuid.uuid4()
        row = _delivery_order(business_id, uuid.uuid4(), status="ACCEPTED", order_id=order_id)
        order = Order(id=order_id, business_id=business_id, status="ACCEPTED", order_number="UE-order-1")

        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(Order, order)

        await _handle_cancellation(
            db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1"
        )

        assert row.status == "CANCELLED"
        assert order.status == "CANCELLED"
        assert db.commits == 1
        # No stock-reversal call is made anywhere in this path — a financial
        # cancellation is not a production fact (DECISIONS.md).

    async def test_already_terminal_cancellation_is_a_no_op(self):
        business_id = uuid.uuid4()
        row = _delivery_order(business_id, uuid.uuid4(), status="DENIED")
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        await _handle_cancellation(
            db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1"
        )
        assert db.commits == 0

    async def test_process_delivery_cancellation_job_delegates_correctly(self):
        session = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        with patch(
            "backend.app.delivery.order_service._handle_cancellation", new=AsyncMock()
        ) as mock_handle:
            await process_delivery_cancellation(ctx, "UBER_EATS", str(uuid.uuid4()), "order-1")
        mock_handle.assert_awaited_once()


# ── Accept-deadline watchdog ──────────────────────────────────────────────────

class TestAcceptDeadlineWatchdog:
    async def test_denies_orders_past_deadline_plus_grace(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        overdue = _delivery_order(
            business_id, connection.id, status="NEEDS_MAPPING",
            accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=700),
        )

        session = TypedFakeDB()
        session.register_select(DeliveryOrder, ScalarResult(rows=[overdue]))
        session.register_get(DeliveryConnection, connection)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock()
        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await delivery_accept_deadline_watchdog(ctx)

        fake_adapter.deny_order.assert_awaited_once()
        assert overdue.status == "DENIED"
        assert session.commits == 1

    async def test_never_auto_accepts_anything(self):
        """The watchdog's only possible mutation is a deny — confirmed by construction:
        it never calls accept_order anywhere in its implementation."""
        import inspect
        from backend.app.delivery import order_service
        source = inspect.getsource(order_service.delivery_accept_deadline_watchdog)
        assert "accept_order" not in source

    async def test_query_has_sql_side_deadline_predicate_and_a_limit(self):
        """
        BUG-0093: previously the SELECT had no deadline comparison at all
        (filtering happened only in Python afterwards) and no LIMIT, so
        EVERY open delivery order platform-wide was loaded and row-locked
        on every tick. Both must now be in the SQL itself.
        """
        from sqlalchemy.dialects import postgresql
        from backend.app.delivery import order_service

        captured_stmt = {}

        class CapturingDB(TypedFakeDB):
            async def execute(self, stmt):
                captured_stmt["stmt"] = stmt
                return await super().execute(stmt)

        db = CapturingDB()
        db.register_select(DeliveryOrder, ScalarResult(rows=[]))

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=db)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        await delivery_accept_deadline_watchdog(ctx)

        compiled = str(captured_stmt["stmt"].compile(dialect=postgresql.dialect()))
        assert "LIMIT" in compiled
        assert "accept_deadline_at <=" in compiled
        assert order_service._WATCHDOG_BATCH_LIMIT <= 500

    async def test_network_deny_call_happens_only_after_the_local_claim_is_committed(self):
        """
        BUG-0093: the outbound deny_pos_order network call must happen
        OUTSIDE the row-locked transaction — a slow/hung call for one row
        must never hold the lock (and a worker DB connection) for another.
        Proven here by asserting the claim's own commit has already
        happened by the time the (mocked) network call fires.
        """
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        overdue = _delivery_order(
            business_id, connection.id, status="NEEDS_MAPPING",
            accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=700),
        )
        session = TypedFakeDB()
        session.register_select(DeliveryOrder, ScalarResult(rows=[overdue]))
        session.register_get(DeliveryConnection, connection)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        commits_seen_at_call_time = []

        async def _deny(*a, **k):
            commits_seen_at_call_time.append(session.commits)

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock(side_effect=_deny)
        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await delivery_accept_deadline_watchdog(ctx)

        assert commits_seen_at_call_time == [1], (
            "the local DENIED claim must already be committed before the network call is made"
        )
        assert overdue.status == "DENIED"
        assert overdue.deny_reason_code == "OTHER"

    async def test_one_rows_deny_call_failure_does_not_affect_another_rows_local_claim(self):
        """Each claimed row is already terminal in the DB regardless of its own network call's outcome."""
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        overdue_1 = _delivery_order(
            business_id, connection.id, status="NEEDS_MAPPING", external_order_id="order-1",
            accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=700),
        )
        overdue_2 = _delivery_order(
            business_id, connection.id, status="RECEIVED", external_order_id="order-2",
            accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=700),
        )
        session = TypedFakeDB()
        session.register_select(DeliveryOrder, ScalarResult(rows=[overdue_1, overdue_2]))
        session.register_get(DeliveryConnection, connection, connection)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        fake_adapter = MagicMock()
        fake_adapter.deny_order = AsyncMock(
            side_effect=[DeliveryProviderError("UBER_EATS", "network blip"), None]
        )
        with patch("backend.app.delivery.order_service.registry.get_provider", return_value=fake_adapter):
            await delivery_accept_deadline_watchdog(ctx)

        assert overdue_1.status == "DENIED"
        assert overdue_2.status == "DENIED"
        assert session.commits == 1, "the claim commit happens once, before any network call"

    async def test_orders_still_within_grace_are_left_alone(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id)
        not_yet_due = _delivery_order(
            business_id, connection.id, status="RECEIVED",
            accept_deadline_at=datetime.now(timezone.utc) - timedelta(seconds=10),  # under the 90s grace
        )
        session = TypedFakeDB()
        session.register_select(DeliveryOrder, ScalarResult(rows=[not_yet_due]))

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        ctx = {"session_factory": MagicMock(return_value=cm)}

        await delivery_accept_deadline_watchdog(ctx)
        assert not_yet_due.status == "RECEIVED"
        assert session.commits == 0
