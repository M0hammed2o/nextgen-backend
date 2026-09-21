"""
build_line_specs_from_bot_cart — the WhatsApp bot's equivalent of POS's
build_line_specs (tests/test_inventory_pos_integration.py::TestBuildLineSpecs),
adapting the plain-dict cart format state_machine.add_to_cart() produces
into the same LineItemSpec the stock resolver consumes. Pure, no DB.
"""
import uuid

from backend.app.inventory.ledger import build_line_specs_from_bot_cart


def _cart_item(menu_item_id, quantity=1, selected_options=None, add_ons=None):
    return {
        "menu_item_id": str(menu_item_id),
        "name": "Test Item",
        "price_cents": 1000,
        "quantity": quantity,
        "selected_options": selected_options or [],
        "add_ons": add_ons or [],
    }


class TestBuildLineSpecsFromBotCart:
    def test_reads_menu_item_id_and_quantity(self):
        item_id = uuid.uuid4()
        specs = build_line_specs_from_bot_cart([_cart_item(item_id, quantity=3)])
        assert len(specs) == 1
        assert specs[0].menu_item_id == item_id
        assert specs[0].quantity == 3

    def test_reads_selected_options_the_same_way_as_pos(self):
        specs = build_line_specs_from_bot_cart([
            _cart_item(uuid.uuid4(), selected_options=[
                {"group_id": "grp-size", "option_id": "opt-large", "price_delta_cents": 3000}
            ])
        ])
        assert specs[0].selected_option_ids == {"grp-size": "opt-large"}

    def test_reads_add_ons_and_quantities_the_same_way_as_pos(self):
        add_on_id = uuid.uuid4()
        specs = build_line_specs_from_bot_cart([
            _cart_item(uuid.uuid4(), add_ons=[
                {"add_on_id": str(add_on_id), "name": "Extra Cheese",
                 "price_cents": 1500, "quantity": 2}
            ])
        ])
        assert specs[0].add_on_selections[0].add_on_id == add_on_id
        assert specs[0].add_on_selections[0].quantity == 2

    def test_removed_ref_ids_is_always_empty(self):
        """
        The WhatsApp bot has no structured removed-ingredient concept today
        (captured as free-text special_instructions only, same as
        order_creator.py never setting removed_ingredients_snapshot) — this
        must stay an empty tuple, never fabricated.
        """
        specs = build_line_specs_from_bot_cart([_cart_item(uuid.uuid4())])
        assert specs[0].removed_ref_ids == ()

    def test_item_with_no_menu_item_id_is_skipped_not_crashed(self):
        item = _cart_item(uuid.uuid4())
        item["menu_item_id"] = None
        specs = build_line_specs_from_bot_cart([item])
        assert specs == []

    def test_malformed_menu_item_id_is_skipped_not_crashed(self):
        item = _cart_item(uuid.uuid4())
        item["menu_item_id"] = "not-a-uuid"
        specs = build_line_specs_from_bot_cart([item])
        assert specs == []

    def test_malformed_add_on_id_is_skipped_not_crashed(self):
        specs = build_line_specs_from_bot_cart([
            _cart_item(uuid.uuid4(), add_ons=[{"add_on_id": "not-a-uuid", "name": "X"}])
        ])
        assert specs[0].add_on_selections == ()

    def test_empty_cart_produces_no_specs(self):
        assert build_line_specs_from_bot_cart([]) == []

    def test_missing_quantity_key_defaults_to_one(self):
        item = _cart_item(uuid.uuid4())
        del item["quantity"]
        specs = build_line_specs_from_bot_cart([item])
        assert specs[0].quantity == 1
