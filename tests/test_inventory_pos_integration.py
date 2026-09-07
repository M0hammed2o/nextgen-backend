"""
POS <-> inventory integration, and receipt rendering.

Mocked-session tests in the style of the existing POS suite, covering the
behaviour that the real-database end-to-end run (scripts/verify_ciao_e2e.py)
exercises against live Postgres. These run everywhere, with no container.
"""

import types
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.inventory.ledger import (
    build_line_specs,
    build_movement,
    reversal_idempotency_key,
    sale_idempotency_key,
)
from backend.app.inventory.units import cost_per_canonical_micro_cents, to_milli
from backend.app.receipts.document import build_receipt_document, columns_for
from backend.app.receipts.escpos import (
    PrinterCapabilities,
    format_money,
    render_escpos,
    render_text,
    two_column,
    wrap,
)
from shared.enums import StockMovementType
from shared.pricing.engine import apply_vat_to_total, calculate_vat_cents


# ── Idempotency keys ─────────────────────────────────────────────────────────

class TestIdempotencyKeys:
    def test_sale_key_is_scoped_per_ingredient(self):
        """
        One sale posts one movement PER INGREDIENT, so the key must include
        the ingredient — a single key per order would collide with itself on
        the (business_id, idempotency_key) unique index.
        """
        order = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        assert sale_idempotency_key(order, a) != sale_idempotency_key(order, b)

    def test_sale_key_is_stable_for_the_same_pair(self):
        order, ingredient = uuid.uuid4(), uuid.uuid4()
        assert sale_idempotency_key(order, ingredient) == sale_idempotency_key(order, ingredient)

    def test_reversal_key_never_collides_with_its_sale(self):
        """A void must be able to coexist with the sale it reverses."""
        order, ingredient = uuid.uuid4(), uuid.uuid4()
        assert sale_idempotency_key(order, ingredient) != reversal_idempotency_key(
            order, ingredient
        )


# ── Movement construction ────────────────────────────────────────────────────

class TestBuildMovement:
    def test_value_is_computed_from_the_snapshotted_unit_cost(self):
        cost = cost_per_canonical_micro_cents(19000, to_milli(2, "kg"))  # R95/kg
        movement = build_movement(
            business_id=uuid.uuid4(),
            ingredient_id=uuid.uuid4(),
            quantity_milli=-to_milli(150, "g"),
            movement_type=StockMovementType.SALE_CONSUMPTION.value,
            unit_cost_micro_cents=cost,
        )
        assert movement.quantity_milli == -150_000
        assert movement.value_cents == -1425  # signed, R14.25 leaving

    def test_occurred_at_defaults_to_now_but_can_be_backdated(self):
        """
        occurred_at is the business event time a stocktake cut-off compares
        against, so it must be settable independently of insertion time.
        """
        when = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        movement = build_movement(
            business_id=uuid.uuid4(),
            ingredient_id=uuid.uuid4(),
            quantity_milli=1000,
            movement_type=StockMovementType.GOODS_RECEIVED.value,
            occurred_at=when,
        )
        assert movement.occurred_at == when
        assert movement.created_at >= when


# ── Adapting POS results into stock specs ────────────────────────────────────

def _resolved_item(menu_item_id, quantity=1, options=None, add_ons=None, removed=None):
    return types.SimpleNamespace(
        menu_item_id=menu_item_id,
        breakdown=types.SimpleNamespace(quantity=quantity),
        selected_options_snapshot=options or [],
        add_ons_snapshot=add_ons or [],
        removed_ingredients_snapshot=removed or [],
    )


class TestBuildLineSpecs:
    def test_reads_options_from_the_priced_snapshot(self):
        """
        Stock must be derived from the SAME validated snapshot the customer
        was charged against, never from the raw client request.
        """
        item_id = uuid.uuid4()
        specs = build_line_specs([
            _resolved_item(
                item_id,
                quantity=3,
                options=[{"group_id": "g1", "option_id": "o1", "option_name": "Large"}],
            )
        ])
        assert len(specs) == 1
        assert specs[0].menu_item_id == item_id
        assert specs[0].quantity == 3
        assert specs[0].selected_option_ids == {"g1": "o1"}

    def test_reads_add_ons_and_quantities(self):
        add_on_id = uuid.uuid4()
        specs = build_line_specs([
            _resolved_item(
                uuid.uuid4(),
                add_ons=[{"add_on_id": str(add_on_id), "name": "Extra Cheese", "quantity": 2}],
            )
        ])
        assert specs[0].add_on_selections[0].add_on_id == add_on_id
        assert specs[0].add_on_selections[0].quantity == 2

    def test_reads_removals(self):
        specs = build_line_specs([
            _resolved_item(uuid.uuid4(), removed=[{"id": "rem-1", "name": "Mushroom"}])
        ])
        assert specs[0].removed_ref_ids == ("rem-1",)

    def test_malformed_add_on_id_is_skipped_not_crashed(self):
        """A bad snapshot must never take the till down mid-service."""
        specs = build_line_specs([
            _resolved_item(uuid.uuid4(), add_ons=[{"add_on_id": "not-a-uuid", "name": "X"}])
        ])
        assert specs[0].add_on_selections == ()

    def test_empty_snapshots_produce_an_empty_spec(self):
        specs = build_line_specs([_resolved_item(uuid.uuid4())])
        assert specs[0].selected_option_ids == {}
        assert specs[0].add_on_selections == ()
        assert specs[0].removed_ref_ids == ()


# ── VAT ──────────────────────────────────────────────────────────────────────

class TestVat:
    def test_inclusive_extracts_vat_without_changing_the_total(self):
        """Switching VAT on must never silently reprice a tenant's menu."""
        total, vat = apply_vat_to_total(11500, 1500, True)
        assert (total, vat) == (11500, 1500)

    def test_exclusive_adds_vat_on_top(self):
        total, vat = apply_vat_to_total(10000, 1500, False)
        assert (total, vat) == (11500, 1500)

    def test_both_modes_agree_on_the_gross(self):
        assert apply_vat_to_total(11500, 1500, True)[0] == apply_vat_to_total(10000, 1500, False)[0]

    def test_unregistered_business_pays_no_vat(self):
        assert apply_vat_to_total(11500, 0, True) == (11500, 0)

    def test_rounding_is_half_up_and_applied_once(self):
        # 999c inclusive at 15% -> 130.30... -> 130
        assert calculate_vat_cents(999, 1500, True) == 130

    def test_zero_and_negative_amounts_are_safe(self):
        assert calculate_vat_cents(0, 1500, True) == 0
        assert calculate_vat_cents(-500, 1500, True) == 0


# ── Receipt layout helpers ───────────────────────────────────────────────────

class TestReceiptLayout:
    def test_paper_widths_map_to_standard_columns(self):
        assert columns_for(58) == 32
        assert columns_for(80) == 48
        assert columns_for(0) == 48  # unknown falls back to 80 mm

    def test_two_column_fills_the_width(self):
        line = two_column("Subtotal", "R120.00", 32)
        assert len(line) == 32
        assert line.startswith("Subtotal")
        assert line.endswith("R120.00")

    def test_two_column_truncates_the_label_never_the_amount(self):
        """An amount is the one thing on a receipt that must never be clipped."""
        line = two_column("A" * 100, "R1234.56", 32)
        assert line.endswith("R1234.56")
        assert len(line) == 32

    def test_wrap_hard_splits_a_word_longer_than_the_line(self):
        assert wrap("X" * 40, 10) == ["X" * 10] * 4

    def test_money_formatting(self):
        assert format_money(1425) == "R14.25"
        assert format_money(-800) == "-R8.00"
        assert format_money(0) == "R0.00"


# ── Receipt document ─────────────────────────────────────────────────────────

def _business(**overrides):
    defaults = dict(
        name="Ciao Pizza", address="Lifestyle Centre", phone="032 946 0000",
        vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500,
        vat_inclusive_pricing=True, receipt_paper_width_mm=80, receipt_copies=1,
        currency="ZAR", receipt_config_json={"footer_message": "Grazie!"},
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _order(**overrides):
    item = types.SimpleNamespace(
        quantity=2, name_snapshot="Large Chicken Pizza", line_total_cents=25800,
        selected_options_snapshot=[{"group_id": "g", "option_id": "o",
                                    "option_name": "Large", "price_delta_cents": 3000}],
        add_ons_snapshot=[{"name": "Extra Cheese", "quantity": 1, "price_cents": 1500}],
        removed_ingredients_snapshot=[{"id": "r1", "name": "Mushroom"}],
        special_instructions="Well done",
    )
    defaults = dict(
        order_number="A-0042", created_at=datetime(2026, 9, 7, 18, 32, tzinfo=timezone.utc),
        order_mode="DINE_IN", table_number="7", customer_name="Thabo",
        subtotal_cents=25800, discount_cents=800, delivery_fee_cents=0,
        tax_cents=3261, total_cents=25000, currency="ZAR",
        payment_method="CASH", payment_status="PAID",
        cash_tendered_cents=30000, change_due_cents=5000,
        payment_reference=None, refund_amount_cents=None, items=[item],
        created_by_user_id=None,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestReceiptDocument:
    def test_renders_at_both_supported_widths(self):
        for width, columns in ((58, 32), (80, 48)):
            document = build_receipt_document(_order(), _business(receipt_paper_width_mm=width))
            assert document.columns == columns
            for line in render_text(document).split("\n"):
                assert len(line) <= columns, f"{width}mm line overflows: {line!r}"

    def test_shows_vat_number_and_tax_invoice_label_when_registered(self):
        text = render_text(build_receipt_document(_order(), _business()))
        assert "VAT No: 4123456789" in text
        assert "TAX INVOICE" in text
        assert "VAT @ 15.00% (incl)" in text

    def test_hides_vat_entirely_when_not_registered(self):
        business = _business(vat_registered=False, vat_number=None)
        text = render_text(build_receipt_document(_order(tax_cents=0), business))
        assert "VAT" not in text
        assert "TAX INVOICE" not in text

    def test_shows_modifiers_add_ons_and_removals(self):
        text = render_text(build_receipt_document(_order(), _business()))
        assert "Large" in text
        assert "+ Extra Cheese" in text
        assert "No Mushroom" in text
        assert "Well done" in text

    def test_shows_cash_and_change(self):
        text = render_text(build_receipt_document(_order(), _business()))
        assert "R300.00" in text
        assert "R50.00" in text

    def test_kitchen_slip_carries_no_prices(self):
        document = build_receipt_document(_order(), _business(), document_type="KITCHEN")
        text = render_text(document)
        assert "KITCHEN" in text
        assert "R258.00" not in text
        assert "TOTAL" not in text
        assert "Large Chicken Pizza" in text  # but still says what to make

    def test_reprint_is_visibly_marked(self):
        document = build_receipt_document(_order(), _business(), is_reprint=True)
        assert document.banner == "REPRINT"
        assert "*** REPRINT ***" in render_text(document)

    def test_void_and_refund_banners(self):
        assert build_receipt_document(
            _order(payment_status="VOIDED"), _business()
        ).banner == "SALE VOIDED"
        assert build_receipt_document(
            _order(payment_status="REFUNDED"), _business()
        ).banner == "REFUNDED"

    def test_franchise_name_is_not_printed_twice_when_it_matches_trading_name(self):
        business = _business(
            receipt_config_json={"trading_name": "Ciao Pizza", "franchise_name": "ciao pizza"}
        )
        assert render_text(build_receipt_document(_order(), business)).count("Ciao Pizza") == 1

    def test_missing_optional_config_still_renders(self):
        """A tenant that has configured nothing must still get a usable slip."""
        bare = types.SimpleNamespace(
            name="Bare Cafe", address=None, phone=None, vat_registered=False,
            vat_number=None, vat_rate_basis_points=0, vat_inclusive_pricing=True,
            receipt_paper_width_mm=80, receipt_copies=1, currency="ZAR",
            receipt_config_json=None,
        )
        text = render_text(build_receipt_document(_order(tax_cents=0), bare))
        assert "Bare Cafe" in text
        assert "TOTAL" in text


# ── ESC/POS ──────────────────────────────────────────────────────────────────

class TestEscPos:
    def test_starts_with_the_initialise_command(self):
        payload = render_escpos(build_receipt_document(_order(), _business()))
        assert payload.startswith(b"\x1b@")

    def test_full_capability_printer_gets_cut_and_drawer(self):
        payload = render_escpos(
            build_receipt_document(_order(), _business()), open_drawer=True
        )
        assert b"\x1dV" in payload      # cut
        assert b"\x1bp\x00" in payload  # drawer kick

    def test_limited_printer_receives_no_unsupported_bytes(self):
        """
        The whole point of capability detection: a printer that cannot cut or
        kick a drawer must not receive those bytes and print them as garbage.
        """
        minimal = PrinterCapabilities(
            supports_cut=False, supports_drawer=False, supports_qr=False,
            supports_bold=False, supports_double_height=False,
        )
        payload = render_escpos(
            build_receipt_document(_order(), _business()), minimal, open_drawer=True
        )
        assert b"\x1dV" not in payload
        assert b"\x1bp" not in payload
        assert b"\x1bE\x01" not in payload

    def test_drawer_only_fires_when_asked(self):
        payload = render_escpos(
            build_receipt_document(_order(), _business()), open_drawer=False
        )
        assert b"\x1bp\x00" not in payload

    def test_qr_only_emitted_when_configured_and_supported(self):
        with_qr = _business(receipt_config_json={"qr_data": "https://ciaopizza.co.za"})
        assert b"\x1d(k" in render_escpos(build_receipt_document(_order(), with_qr))
        assert b"\x1d(k" not in render_escpos(build_receipt_document(_order(), _business()))

    def test_unencodable_characters_never_break_a_print(self):
        """A customer name the codepage lacks must not stop the till printing."""
        payload = render_escpos(
            build_receipt_document(_order(customer_name="Zoë 日本"), _business())
        )
        assert payload.startswith(b"\x1b@")
        assert len(payload) > 100

    def test_escpos_and_text_carry_the_same_content(self):
        """The two renderers share one layout, so they cannot drift apart."""
        document = build_receipt_document(_order(), _business())
        text = render_text(document)
        payload = render_escpos(document, PrinterCapabilities(codepage="utf-8"))
        for token in ("Ciao Pizza", "A-0042", "TOTAL", "Grazie!"):
            assert token in text
            assert token.encode("utf-8") in payload
