"""
Layer 3 (WhatsApp ordering reliability directive, 2026-10-26): adversarial
testing at the AI-response boundary. No existing test file covered
llm_parser.py's parse_llm_response/parse_items_response at all before this
-- a genuine coverage gap, given this is the exact seam where a real LLM's
imperfect output becomes structured data the backend trusts.

Every test here feeds a RAW STRING (exactly what a real model's text
response would look like) through the actual parser -- never a pre-built
Python dict -- so a change to the regex/JSON-extraction logic itself would
actually be exercised, not just the dataclass construction.

The core contract under test: the parser must NEVER raise on malformed
input, and must never silently promote clearly-invalid data (a null name,
a missing/garbage quantity) into something that looks valid to a caller
that isn't checking carefully. Downstream quantity clamping (the existing
`max(1, min(int(qty or 1), 20))` pattern used at every known commit site)
is a separate, already-covered concern -- these tests target the parser
itself.
"""
import pytest

from backend.app.bot.llm_parser import parse_items_response, parse_llm_response


class TestMalformedJSON:
    def test_truncated_json_block_does_not_raise(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger", "quant'
        result = parse_llm_response(raw)
        assert result.action == "chitchat"
        assert result.items == []

    def test_unterminated_string_does_not_raise(self):
        raw = '{"action": "add_items", "message": "Added the burger, "items": []}'
        result = parse_llm_response(raw)
        assert result.action == "chitchat"

    def test_trailing_comma_does_not_raise(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger", "quantity": 1,}],}\n```'
        result = parse_llm_response(raw)
        # Either gracefully falls back to chitchat, or (if the loose brace
        # scanner below the code-fence regex happens to recover a valid
        # subset) parses -- either way, must not raise and must not invent
        # an item that wasn't really there.
        assert result.action in ("chitchat", "add_items")

    def test_completely_non_json_garbage_text(self):
        raw = "I'm sorry, I didn't understand that. Could you clarify what you'd like?"
        result = parse_llm_response(raw)
        assert result.action == "chitchat"
        assert result.items == []
        assert "clarify" in result.message.lower()

    def test_empty_string_does_not_raise(self):
        result = parse_llm_response("")
        assert result.action == "chitchat"
        assert result.items == []

    def test_only_whitespace_does_not_raise(self):
        result = parse_llm_response("   \n\n   ")
        assert result.action == "chitchat"

    def test_json_array_instead_of_object_falls_back(self):
        # A model that forgets the outer {} and just emits a bare array.
        raw = '```json\n[{"name": "Burger", "quantity": 1}]\n```'
        result = parse_llm_response(raw)
        assert result.action == "chitchat"
        assert result.items == []

    def test_deeply_nested_garbage_does_not_raise(self):
        raw = '{"action": {"nested": {"too": {"deep": "add_items"}}}, "items": [[[1,2,3]]]}'
        result = parse_llm_response(raw)
        # `action` resolving to a dict instead of a string is itself
        # invalid-but-truthy data -- the parser must not crash constructing
        # the response, whatever it decides to do with it.
        assert result is not None


class TestHallucinatedAndMissingFields:
    def test_item_with_null_name_is_not_silently_treated_as_valid(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": null, "quantity": 2}]}\n```'
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        assert len(result.items) == 1
        # The item IS structurally present (caller's job to skip it), but
        # its name must come through as None, never a fabricated string --
        # a caller checking `if not pi.name: skip` (the real pipeline.py
        # pattern, confirmed in this codebase) correctly ignores it.
        assert result.items[0].name is None

    def test_missing_quantity_defaults_to_one_not_zero_or_none(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger"}]}\n```'
        result = parse_llm_response(raw)
        assert result.items[0].quantity == 1

    def test_missing_action_field_defaults_to_chitchat_not_a_mutation(self):
        raw = '```json\n{"message": "Here is the menu", "items": []}\n```'
        result = parse_llm_response(raw)
        assert result.action == "chitchat", (
            "a response with no action field must never be treated as a cart mutation"
        )

    def test_hallucinated_quantity_as_string_does_not_raise(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger", "quantity": "a lot"}]}\n```'
        # Real downstream commit sites do int(pi.quantity or 1) -- a
        # non-numeric string here must surface as a clear, catchable
        # failure at that point, not something this parser silently
        # coerces into a plausible-looking number.
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        with pytest.raises((ValueError, TypeError)):
            int(result.items[0].quantity)

    def test_negative_quantity_passes_through_for_downstream_clamping(self):
        # The parser itself does not clamp -- every real commit site in
        # pipeline.py does `max(1, min(int(qty or 1), 20))`. Confirm the
        # parser doesn't mask the bad value by pre-clamping it, which
        # would make that downstream safety net untestable in isolation.
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger", "quantity": -5}]}\n```'
        result = parse_llm_response(raw)
        assert result.items[0].quantity == -5

    def test_absurd_quantity_passes_through_for_downstream_clamping(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Burger", "quantity": 999999}]}\n```'
        result = parse_llm_response(raw)
        assert result.items[0].quantity == 999999

    def test_item_entry_that_is_not_a_dict_is_skipped_not_crashed_on(self):
        raw = '```json\n{"action": "add_items", "items": ["Burger", {"name": "Chips", "quantity": 1}, 42, null]}\n```'
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        # Only the genuine dict entry should survive.
        assert len(result.items) == 1
        assert result.items[0].name == "Chips"

    def test_add_ons_entries_that_are_not_dicts_are_dropped(self):
        raw = (
            '```json\n{"action": "add_items", "items": [{"name": "Burger", '
            '"quantity": 1, "add_ons": ["Extra Cheese", {"name": "Bacon"}, null, 5]}]}\n```'
        )
        result = parse_llm_response(raw)
        assert len(result.items[0].add_ons) == 1
        assert result.items[0].add_ons[0]["name"] == "Bacon"


class TestContradictoryFields:
    def test_add_items_action_with_empty_items_list(self):
        """A model that says 'add_items' but forgot to list anything --
        must not be treated as if real items were added."""
        raw = '```json\n{"action": "add_items", "message": "Added your item!", "items": []}\n```'
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        assert result.items == []

    def test_replace_item_with_remove_and_add_both_missing(self):
        raw = '```json\n{"action": "replace_item", "items": [{"name": null, "quantity": 1}]}\n```'
        result = parse_llm_response(raw)
        assert result.action == "replace_item"
        assert result.items[0].remove is None
        assert result.items[0].add is None

    def test_unrecognised_action_string_passes_through_literally(self):
        # The parser doesn't validate `action` against a fixed enum --
        # confirms that contract explicitly, since the real guard (in
        # pipeline.py's "order-mutation guard") depends on checking the
        # literal string against a known-action set, not on this parser
        # rejecting unknown values itself.
        raw = '```json\n{"action": "delete_entire_order_and_refund", "items": []}\n```'
        result = parse_llm_response(raw)
        assert result.action == "delete_entire_order_and_refund"

    def test_message_and_items_disagree_in_count_both_preserved_as_given(self):
        # "Added 3 items!" but items=[] -- the parser must not try to
        # reconcile or invent matching items; that reconciliation is
        # pipeline.py's job (the order-mutation guard / BUG-0159
        # reconciliation), not this layer's.
        raw = '```json\n{"action": "add_items", "message": "Added 3 items to your order!", "items": []}\n```'
        result = parse_llm_response(raw)
        assert result.items == []
        assert "3 items" in result.message


class TestItemsResponseAdversarial:
    """parse_items_response has a separate, simpler contract (expects a
    bare JSON array) -- same adversarial pressure applied."""

    def test_malformed_array_returns_empty_not_raise(self):
        assert parse_items_response("[{broken") == []

    def test_non_array_json_returns_empty(self):
        assert parse_items_response('{"not": "an array"}') == []

    def test_empty_string_returns_empty(self):
        assert parse_items_response("") == []

    def test_array_with_mixed_valid_and_garbage_entries(self):
        raw = '[{"name": "Chips", "quantity": 2}, "garbage", null, 7]'
        result = parse_items_response(raw)
        assert len(result) == 1
        assert result[0].name == "Chips"
        assert result[0].quantity == 2


class TestInjectionLikeContent:
    """A customer or a compromised/confused model embedding text that
    looks like it's trying to manipulate parsing/formatting -- must be
    treated as inert data, never executed or specially interpreted."""

    def test_message_field_containing_literal_json_braces_is_just_text(self):
        raw = '```json\n{"action": "chitchat", "message": "We don\'t sell {fake items}", "items": []}\n```'
        result = parse_llm_response(raw)
        assert result.action == "chitchat"
        assert "fake items" in result.message

    def test_item_name_containing_quote_characters_does_not_break_parsing(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "Mom\\u0027s \\"Famous\\" Burger", "quantity": 1}]}\n```'
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        assert "Famous" in result.items[0].name

    def test_extremely_long_item_name_does_not_raise(self):
        raw = '```json\n{"action": "add_items", "items": [{"name": "%s", "quantity": 1}]}\n```' % ("A" * 5000)
        result = parse_llm_response(raw)
        assert result.action == "add_items"
        assert len(result.items[0].name) == 5000
