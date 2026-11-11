"""
BUG-0189: Uber allergy requests must reach the kitchen, or the order must be denied.

Uber's Quality & Performance guide (Required): "Reject orders when allergens
or special instructions are provided but cannot be relayed to the POS."
Allergy requests arrive per item in `cart.items[].special_requests[].allergy`
(`allergens_to_exclude[]` with `type`/`freeform_text`, plus
`allergy_instructions`).
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.delivery.canonical import CanonicalDeliveryLine
from backend.app.delivery.order_service import _handle_order, _kitchen_note, _line_note, _ResolvedDeliveryLine
from backend.app.delivery.uber.mapping import parse_order_details
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order, OrderItem
from tests.test_delivery_order_service import (
    ScalarResult,
    TypedFakeDB,
    _business,
    _canonical_order,
    _connection,
    _delivery_order,
    _menu_item,
)


def _raw(item_extra: dict | None = None, cart_extra: dict | None = None) -> dict:
    item = {"id": "uber-item-1", "title": "Best Burger", "quantity": 1,
            "price": {"unit_price": {"amount": 7500}, "total_price": {"amount": 7500}}, **(item_extra or {})}
    return {"id": "order-1", "display_id": "ABC12", "current_state": "CREATED",
            "cart": {"items": [item], **(cart_extra or {})},
            "payment": {"charges": {"sub_total": {"amount": 7500}, "total": {"amount": 7500}}}}


ALLERGY = {"special_requests": [{"allergy": {
    "allergens_to_exclude": [{"type": "DAIRY"}, {"type": "TREE_NUTS"}, {"type": "OTHER", "freeform_text": "sesame"}],
    "allergy_instructions": "Severe - please use clean gloves",
}}]}


class TestParse:
    def test_allergens_and_instructions_are_read(self):
        line = parse_order_details(_raw(ALLERGY), external_store_id="s").lines[0]
        assert line.allergens == ("Dairy", "Tree nuts", "sesame")
        assert line.allergy_instructions == "Severe - please use clean gloves"
        assert line.has_allergy_request and not line.special_requests_unreadable

    def test_single_object_shape_is_read_too(self):
        raw = _raw({"special_requests": {"allergy": {"allergens_to_exclude": [{"type": "PEANUTS"}]}}})
        order = parse_order_details(raw, external_store_id="s")
        assert order.lines[0].allergens == ("Peanuts",) and order.has_allergy_request

    def test_no_special_requests_is_not_an_allergy_order(self):
        order = parse_order_details(_raw(), external_store_id="s")
        assert not order.has_allergy_request and not order.special_requests_unreadable

    def test_unreadable_shapes_are_flagged_never_dropped(self):
        for extra in (
            {"special_requests": ["nuts"]},
            {"special_requests": [{"allergy": "nuts"}]},
            {"special_requests": [{"something_new": {"x": 1}}]},
            {"special_requests": [{"allergy": {"allergens_to_exclude": [{}]}}]},
        ):
            order = parse_order_details(_raw(extra), external_store_id="s")
            assert order.special_requests_unreadable, extra

    def test_cart_level_special_requests_are_unreadable(self):
        order = parse_order_details(_raw(cart_extra={"special_requests": [{"allergy": {}}]}), external_store_id="s")
        assert order.special_requests_unreadable


class TestTicket:
    def test_kitchen_note_leads_with_the_allergy_for_every_line(self):
        order = parse_order_details(_raw(ALLERGY), external_store_id="s")
        note = _kitchen_note("UBER_EATS", order)
        assert note.startswith("ALLERGY ALERT: Best Burger: exclude Dairy, Tree nuts, sesame")
        assert "clean gloves" in note

    def test_line_note_carries_the_allergy(self):
        canonical_line = parse_order_details(_raw(ALLERGY), external_store_id="s").lines[0]
        note = _line_note(_ResolvedDeliveryLine(canonical=canonical_line, menu_item_id=uuid.uuid4(),
                                                menu_item_name="Best Burger"))
        assert note.startswith("ALLERGY: exclude Dairy, Tree nuts, sesame")


def _mapped_db(business_id, connection, business, row, menu_item) -> TypedFakeDB:
    db = TypedFakeDB()
    db.register_select(DeliveryOrder, ScalarResult(scalar=row))
    db.register_get(DeliveryConnection, connection)
    db.register_get(Business, business)
    db.register_select(Order, ScalarResult(rows=[]))
    db.register_select(ExternalProductMapping, ScalarResult(rows=[ExternalProductMapping(
        id=uuid.uuid4(), business_id=business_id, platform="UBER_EATS", kind="ITEM",
        external_text=menu_item.name, external_key=menu_item.name.lower(),
        external_id="uber-item-1", menu_item_id=menu_item.id, status="MAPPED",
    )]))
    db.register_select(MenuItem, ScalarResult(rows=[menu_item]))
    db.register_select(MenuAddOn, ScalarResult(rows=[]))
    return db


def _allergy_line(menu_item, **extra) -> CanonicalDeliveryLine:
    return CanonicalDeliveryLine(external_item_id="uber-item-1", title=menu_item.name, quantity=1,
                                 unit_price_cents=7500, **extra)


async def _run(db, canonical, *, business_id, force_accept=False):
    adapter = MagicMock()
    adapter.fetch_order = AsyncMock(return_value=canonical)
    adapter.accept_order = AsyncMock()
    adapter.deny_order = AsyncMock()
    with patch("backend.app.delivery.order_service.registry.get_provider", return_value=adapter), \
         patch("backend.app.delivery.order_service.post_sale_consumption", new=AsyncMock()), \
         patch("backend.app.delivery.order_service._publish_alert", new=AsyncMock()) as alert:
        await _handle_order(db, provider="UBER_EATS", business_id=business_id, external_order_id="order-1",
                            event_type=None, force_accept=force_accept)
    return adapter, alert


class TestIntake:
    async def test_allergy_order_is_never_auto_accepted(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)
        db = _mapped_db(business_id, connection, _business(), row, menu_item)
        canonical = _canonical_order(lines=[_allergy_line(menu_item, allergens=("Dairy",))])

        adapter, alert = await _run(db, canonical, business_id=business_id)

        assert row.status == "READY_TO_ACCEPT"
        adapter.accept_order.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added)
        assert alert.call_args[0][1] == "delivery_ready_to_accept"
        assert row.fetched_order_snapshot_json["has_allergy_request"] is True
        assert row.fetched_order_snapshot_json["lines"][0]["allergens"] == ["Dairy"]

    async def test_order_without_allergy_still_auto_accepts(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)
        db = _mapped_db(business_id, connection, _business(), row, menu_item)

        adapter, _ = await _run(db, _canonical_order(lines=[_allergy_line(menu_item)]), business_id=business_id)

        assert row.status == "ACCEPTED"
        adapter.accept_order.assert_awaited_once()

    async def test_staff_accept_puts_the_allergy_on_the_ticket(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=False)
        row = _delivery_order(business_id, connection.id, status="RECEIVED")  # the accept endpoint re-queues from here
        menu_item = _menu_item(business_id)
        db = _mapped_db(business_id, connection, _business(), row, menu_item)
        canonical = _canonical_order(lines=[_allergy_line(menu_item, allergens=("Peanuts",),
                                                          allergy_instructions="anaphylactic")])

        adapter, _ = await _run(db, canonical, business_id=business_id, force_accept=True)

        assert row.status == "ACCEPTED"
        order = next(o for o in db.added if isinstance(o, Order))
        item = next(o for o in db.added if isinstance(o, OrderItem))
        assert order.special_instructions.startswith("ALLERGY ALERT: Large Margherita: exclude Peanuts")
        assert item.special_instructions.startswith('ALLERGY: exclude Peanuts "anaphylactic"')

    async def test_unreadable_special_request_is_denied_with_special_instructions(self):
        business_id = uuid.uuid4()
        connection = _connection(business_id, auto_accept_enabled=True)
        row = _delivery_order(business_id, connection.id)
        menu_item = _menu_item(business_id)
        db = TypedFakeDB()
        db.register_select(DeliveryOrder, ScalarResult(scalar=row))
        db.register_get(DeliveryConnection, connection)
        db.register_get(Business, _business())
        canonical = _canonical_order(lines=[_allergy_line(menu_item, special_requests_unreadable=True)])

        adapter, alert = await _run(db, canonical, business_id=business_id, force_accept=True)

        adapter.deny_order.assert_awaited_once()
        assert adapter.deny_order.call_args.kwargs["reason_code"] == "SPECIAL_INSTRUCTIONS"
        assert row.status == "DENIED"
        adapter.accept_order.assert_not_awaited()
        assert not any(isinstance(o, Order) for o in db.added)


async def test_accept_tells_uber_which_fields_are_relayed():
    from backend.app.delivery.uber import adapter as uber_adapter

    captured = {}

    async def fake_call(context, fn):
        client = MagicMock()
        with patch.object(uber_adapter.uber_client, "accept_pos_order", new=AsyncMock()) as accept:
            await fn(client, "token", "https://test-api.uber.com")
            captured["body"] = accept.call_args.kwargs["body"]

    with patch.object(uber_adapter, "_call", new=fake_call):
        await uber_adapter.UberEatsAdapter()._accept_order(
            external_order_id="order-1", context=MagicMock(), our_order_id="ours-1")
    assert captured["body"]["fields_relayed"] == {
        "order_special_instructions": True, "item_special_instructions": True,
        "item_special_requests": True, "promotions": False,
    }
    assert captured["body"]["external_reference_id"] == "ours-1"
