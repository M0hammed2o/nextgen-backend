"""
External report adapters — column detection, value parsing, order grouping.

Pure functions, so every rule is testable without a database. This is where a
delivery platform's spreadsheet either becomes correct stock consumption or
silently becomes the wrong thing, so the parsing edge cases matter more than
the happy path.
"""

from datetime import datetime, timezone

from backend.app.external_orders.adapters import (
    adapt_rows,
    detect_columns,
    looks_cancelled,
    normalise_heading,
    order_id_prefix,
    parse_datetime,
    parse_money_cents,
    parse_quantity,
    split_modifiers,
)
from backend.app.external_orders.service import normalise_key
from shared.enums import ExternalPlatform


# ── Heading recognition ──────────────────────────────────────────────────────

class TestHeadingDetection:
    def test_normalisation_strips_punctuation_and_case(self):
        assert normalise_heading("  Order #  ") == "order"
        assert normalise_heading("Item_Name") == "item name"
        assert normalise_heading("Gross-Sales") == "gross sales"

    def test_common_uber_style_headings(self):
        resolved = detect_columns(
            ["Order ID", "Order Date", "Order Time", "Item", "Qty", "Price", "Status"]
        )
        assert resolved["external_order_id"] == "Order ID"
        assert resolved["item"] == "Item"
        assert resolved["quantity"] == "Qty"

    def test_common_mr_d_style_headings(self):
        resolved = detect_columns(
            ["Order Number", "Ordered At", "Product", "Quantity", "Gross", "State"]
        )
        assert resolved["external_order_id"] == "Order Number"
        assert resolved["item"] == "Product"
        assert resolved["quantity"] == "Quantity"
        assert resolved["status"] == "State"

    def test_order_status_does_not_get_mistaken_for_order_id(self):
        """
        A file with both "Order ID" and "Order Status" must not resolve the id
        from the status column — exact alias matches run before substring ones.
        """
        resolved = detect_columns(["Order Status", "Order ID", "Item", "Qty"])
        assert resolved["external_order_id"] == "Order ID"
        assert resolved["status"] == "Order Status"

    def test_a_heading_is_never_claimed_by_two_fields(self):
        resolved = detect_columns(["Order", "Item", "Quantity"])
        assert len(set(resolved.values())) == len(resolved.values())

    def test_unrecognised_headings_are_simply_absent(self):
        resolved = detect_columns(["Courier Tip", "Zone"])
        assert "external_order_id" not in resolved


# ── Value parsing ────────────────────────────────────────────────────────────

class TestMoney:
    def test_rand_symbol(self):
        assert parse_money_cents("R140.00") == 14000

    def test_decimal_comma(self):
        assert parse_money_cents("140,00") == 14000

    def test_thousands_separator(self):
        assert parse_money_cents("1,240.50") == 124050

    def test_plain_number(self):
        assert parse_money_cents(149) == 14900

    def test_blank_is_none_not_zero(self):
        """Absent is not the same as free — a missing price must not read R0."""
        assert parse_money_cents("") is None
        assert parse_money_cents(None) is None

    def test_garbage_is_none(self):
        assert parse_money_cents("n/a") is None


class TestQuantity:
    def test_plain(self):
        assert parse_quantity("3") == 3

    def test_missing_defaults_to_one(self):
        """A per-line export with no quantity column means one of each."""
        assert parse_quantity("") == 1
        assert parse_quantity(None) == 1

    def test_fractional_and_zero_are_rejected(self):
        assert parse_quantity("1.5") is None
        assert parse_quantity("0") is None
        assert parse_quantity("-2") is None

    def test_junk_is_rejected(self):
        assert parse_quantity("two") is None


class TestDateTime:
    def test_iso_with_time(self):
        assert parse_datetime("2026-09-08 18:42") == datetime(2026, 9, 8, 18, 42, tzinfo=timezone.utc)

    def test_separate_date_and_time_columns(self):
        assert parse_datetime("2026-09-08", "18:42") == datetime(
            2026, 9, 8, 18, 42, tzinfo=timezone.utc
        )

    def test_south_african_day_first(self):
        assert parse_datetime("08/09/2026 18:42") == datetime(
            2026, 9, 8, 18, 42, tzinfo=timezone.utc
        )

    def test_iso_t_separator_and_zulu(self):
        assert parse_datetime("2026-09-08T18:42:00Z") == datetime(
            2026, 9, 8, 18, 42, tzinfo=timezone.utc
        )

    def test_trailing_offset_is_tolerated(self):
        assert parse_datetime("2026-09-08 18:42:00+02:00") is not None

    def test_unparseable_returns_none_rather_than_guessing(self):
        """
        Stock must be dated when the sale happened. Defaulting to "now" would
        silently move consumption to the wrong day, so an unreadable timestamp
        has to fail loudly instead.
        """
        assert parse_datetime("last Tuesday") is None
        assert parse_datetime("") is None


class TestModifiers:
    def test_comma_separated(self):
        assert split_modifiers("Extra Cheese, No Mushroom") == ("Extra Cheese", "No Mushroom")

    def test_semicolon_and_pipe_and_newline(self):
        assert split_modifiers("A; B") == ("A", "B")
        assert split_modifiers("A|B") == ("A", "B")
        assert split_modifiers("A\nB") == ("A", "B")

    def test_commas_inside_brackets_are_kept_together(self):
        assert split_modifiers("Toppings (ham, olives); Extra Cheese") == (
            "Toppings (ham, olives)", "Extra Cheese",
        )

    def test_blank(self):
        assert split_modifiers("") == ()
        assert split_modifiers(None) == ()


class TestCancellation:
    def test_recognises_cancelled_variants(self):
        for text in ("Cancelled", "CANCELED", "Refunded", "Order rejected", "Failed"):
            assert looks_cancelled(text), text

    def test_normal_statuses_are_not_cancelled(self):
        for text in ("Delivered", "Completed", "Picked up", None, ""):
            assert not looks_cancelled(text), text


# ── Grouping ─────────────────────────────────────────────────────────────────

def row(order_id, item, qty="1", **extra):
    base = {
        "Order ID": order_id, "Order Date": "2026-09-08", "Order Time": "18:42",
        "Item": item, "Qty": qty, "Price": "149.00", "Status": "Delivered",
    }
    base.update(extra)
    return base


class TestGrouping:
    def test_lines_sharing_an_order_id_become_one_order(self):
        """
        Every export we have specified is one row per LINE. Grouping matters:
        without it, duplicate protection would work per line instead of per
        order, and a three-item order would count as three orders.
        """
        result = adapt_rows([
            row("UE-1", "Large Margherita"),
            row("UE-1", "Coca-Cola"),
            row("UE-2", "Regina"),
        ])
        assert len(result.orders) == 2
        first = next(o for o in result.orders if o.external_order_id == "UE-1")
        assert len(first.lines) == 2
        assert first.item_count == 2

    def test_quantities_sum_into_item_count(self):
        result = adapt_rows([row("UE-1", "Margherita", qty="3")])
        assert result.orders[0].item_count == 3

    def test_gross_accumulates_across_lines(self):
        result = adapt_rows([
            row("UE-1", "A", Price="100.00"),
            row("UE-1", "B", Price="50.00"),
        ])
        assert result.orders[0].gross_cents == 15000

    def test_earliest_timestamp_on_the_order_wins(self):
        """Later lines sometimes carry a fulfilment time, not the order time."""
        result = adapt_rows([
            row("UE-1", "A", **{"Order Time": "19:30"}),
            row("UE-1", "B", **{"Order Time": "18:42"}),
        ])
        assert result.orders[0].placed_at.hour == 18

    def test_missing_order_id_is_an_issue_not_a_silent_drop(self):
        result = adapt_rows([row("", "Margherita")])
        assert result.orders == []
        assert "Missing order ID" in result.issues[0].message

    def test_missing_item_is_reported_with_the_order_id(self):
        result = adapt_rows([row("UE-9", "")])
        assert result.orders == []
        assert "UE-9" in result.issues[0].message

    def test_bad_quantity_is_reported_by_row_number(self):
        result = adapt_rows([row("UE-1", "Margherita", qty="abc")])
        assert result.orders == []
        assert result.issues[0].row == 2  # row 1 is the header

    def test_unreadable_date_rejects_the_order(self):
        result = adapt_rows([
            {"Order ID": "UE-1", "Order Date": "whenever", "Item": "X", "Qty": "1"}
        ])
        assert result.orders == []
        assert "date/time" in result.issues[0].message

    def test_blank_spacer_rows_are_skipped_silently(self):
        result = adapt_rows([
            row("UE-1", "Margherita"),
            {"Order ID": "", "Item": "", "Qty": ""},
        ])
        assert len(result.orders) == 1
        assert result.issues == []

    def test_missing_required_columns_stops_everything(self):
        """Without an id, item and quantity there is nothing to import."""
        result = adapt_rows([{"Something": "x", "Else": "y"}])
        assert result.orders == []
        assert set(result.missing_required) == {"external_order_id", "item", "quantity"}

    def test_manager_override_beats_auto_detection(self):
        rows = [{"Ref": "UE-1", "Thing": "Margherita", "Count": "2",
                 "When": "2026-09-08 18:00"}]
        result = adapt_rows(rows, mapping_override={
            "external_order_id": "Ref", "item": "Thing",
            "quantity": "Count", "order_datetime": "When",
        })
        assert len(result.orders) == 1
        assert result.orders[0].item_count == 2

    def test_empty_file(self):
        assert adapt_rows([]).orders == []


class TestMappingKeys:
    def test_lookup_key_is_case_and_space_insensitive(self):
        """
        Trivial formatting differences must not produce a fresh unresolved
        item every single day.
        """
        assert normalise_key("  Large   Margherita Pizza ") == "large margherita pizza"
        assert normalise_key("LARGE MARGHERITA PIZZA") == normalise_key("large margherita pizza")


class TestOrderNumberPrefix:
    def test_platform_prefixes(self):
        assert order_id_prefix(ExternalPlatform.UBER_EATS.value) == "UE"
        assert order_id_prefix(ExternalPlatform.MR_D.value) == "MRD"
        assert order_id_prefix(ExternalPlatform.GENERIC.value) == "EXT"
