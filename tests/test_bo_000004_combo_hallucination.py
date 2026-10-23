"""
Regression pin for the real production bug behind order BO-000004.

Root cause (see NEXTGEN_DEVELOPMENT_BACKUP.md, entry dated 2026-10-22, and
KNOWN_BUGS.md BUG-0151): when the LLM answers with chitchat/ask_options
prose instead of an add_items action while the cart is still empty,
pipeline.py's "proposed-order capture" step (_handle_with_llm, ~line 3633)
re-runs _extract_items_from_message against the LLM's OWN generated text so
a legitimate single-item proposal ("I'll add an Ice Coffee — shall I
proceed?") can still be committed on "Yes". That same extraction has no
notion of "this phrase is describing a combo's already-included
component" — so when the LLM's fabricated summary explains what a combo
INCLUDES (e.g. "...chips and a Buddy 440ml"), the literal catalog names of
those included components get extracted as if they were independently
proposed line items, while the combo itself (and any invented item name
that doesn't exactly match the catalog) is silently dropped.

The FIX for that mechanism is end-to-end: it cross-checks the LLM's stated
total against what was actually extracted, inside _handle_with_llm itself
— not inside _extract_items_from_message, which correctly keeps doing
literal substring matching regardless (that function has no way to know
"this text is describing an already-paid-for component" on its own, by
design; the check belongs one layer up, where the stated total is
available to compare against). Testing that fix therefore needs the real
pipeline, not a bare call to the extraction function — see
tests/replay/conversations/conv_324.json for the real, end-to-end
reproduction and regression test.

The second, distinct finding below IS correctly testable at the
extraction-function level, since its fix lives inside
_extract_items_from_chunk itself.
"""

import pytest

from backend.app.bot.pipeline import _extract_items_from_message


class _FakeItem:
    def __init__(self, id_, name, price_cents, options_json=None):
        self.id = id_
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False
        self.options_json = options_json or {}


# ── Second, distinct finding, discovered while writing conv_322 ─────────────
# (tests/replay/conversations/): the SAME class of defect also reaches the
# CUSTOMER-FACING deterministic path, not just the LLM-chitchat-proposal
# path above. A combo with a zero-cost "Included Drink" option group
# (tests/replay/fixtures.py::_COMBO_DRINK_CHOICE_OPTIONS) was added to test
# Task 2's "customer can choose their included drink at no extra charge"
# requirement. When the customer's own message names a drink that happens
# to also be a real standalone catalog item (e.g. "Sprite"), the normal
# substring pass in _extract_items_from_chunk finds BOTH matches
# independently in the same chunk — the combo AND a second, separately
# charged "Sprite" line — because nothing marks a word as "already consumed
# as another item's option selection" before the general item-name scan
# runs across the same text. Confirmed directly against pipeline.py's real
# DET_MATCH log output: "added 2 item(s) deterministically:
# ['1x Sprite (330ml)', '1x Smash Burger Combo (Sprite)']", total R140.00
# instead of R120.00.
_COMBO_INCLUDED_DRINK_OPTIONS = {
    "option_groups": [
        {
            "id": "included_drink",
            "name": "Included Drink",
            "required": True,
            "min_selections": 1,
            "max_selections": 1,
            "sort_order": 0,
            "is_enabled": True,
            "options": [
                {"id": "coke", "name": "Coca-Cola", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "sprite", "name": "Sprite", "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
            ],
        },
    ]
}
_COLLISION_CATALOG = [
    _FakeItem(10, "Smash Burger Combo", 12000, options_json=_COMBO_INCLUDED_DRINK_OPTIONS),
    _FakeItem(11, "Sprite", 2000),
]


def test_option_value_colliding_with_standalone_item_name_is_not_double_added():
    extracted = _extract_items_from_message(
        "I'll have the Smash Burger Combo but with Sprite instead",
        _COLLISION_CATALOG,
    )
    names = [item.name for item, _qty, _mod in extracted]

    assert names == ["Smash Burger Combo"], (
        f"Expected only the combo to be extracted (with 'Sprite' captured as "
        f"its included-drink selection, not a separate line), got {names!r}."
    )
