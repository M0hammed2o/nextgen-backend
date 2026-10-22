"""
Regression pin for the real production bug behind order BO-000004.

Root cause (see NEXTGEN_DEVELOPMENT_BACKUP.md, entry dated 2026-10-22, and
KNOWN_BUGS.md): when the LLM answers with chitchat/ask_options prose instead
of an add_items action while the cart is still empty, pipeline.py's
"proposed-order capture" step (_handle_with_llm, ~line 3660) re-runs
_extract_items_from_message against the LLM's OWN generated text so a
legitimate single-item proposal ("I'll add an Ice Coffee — shall I
proceed?") can still be committed on "Yes". That same extraction has no
notion of "this phrase is describing a combo's already-included
component" — so when the LLM's fabricated summary explains what a combo
INCLUDES (e.g. "...chips and a Buddy 440ml"), the literal catalog names of
those included components get extracted as if they were independently
proposed line items, while the combo itself (and any invented item name
that doesn't exactly match the catalog) is silently dropped. The customer
sees and confirms a large, correct-looking order; what actually gets
committed is only the fragments that happened to match real catalog
substrings.

This test does NOT have access to BO-000004's real stored message log (the
production API was unreachable — see the investigation report delivered
2026-10-22). It instead replicates the mechanism directly against the real
_extract_items_from_message function using the real current Mochachos
catalog item names, with a bot-generated summary shaped like the one the
customer reported. It is marked xfail(strict=True): today it genuinely
reproduces the bug (extraction returns the included components as
standalone items), so asserting the DESIRED behaviour fails. Once Task 2's
approved fix lands, this flips to an unexpected pass, and strict=True turns
that into a hard failure — forcing whoever lands the fix to delete the
xfail marker rather than let it decay into a silently-vacuous test.
"""

import pytest

from backend.app.bot.pipeline import _extract_items_from_message


class _FakeItem:
    def __init__(self, id_, name, price_cents):
        self.id = id_
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False


# Real current Mochachos catalog names/prices relevant to this scenario,
# verified via the authenticated owner API on 2026-10-22.
_CATALOG = [
    _FakeItem(1, "Chicken Burger, Beeg Chips and a Buddy", 11990),
    _FakeItem(2, "Chips", 3590),
    _FakeItem(3, "Buddy 440ml", 2890),
]

# Shaped like the bot's actual reported confirmation message for BO-000004:
# a combo explained in terms of its included components, using the
# catalog's own exact item-name strings.
_BOT_SUMMARY_TEXT = (
    "1. Chicken Burger Combo — R119.90\n"
    "- Includes chicken burger, chips and a Buddy 440ml\n"
    "- Hot spice level\n"
    "2. Six Chicken Strips with Chilli Mayo — R99.90\n"
    "Total: R284.60"
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BO-000004: _extract_items_from_message has no concept of "
        "'included component of a combo already being described' — it "
        "extracts Chips and Buddy 440ml as standalone items from the LLM's "
        "own combo-included-components text. Fix pending explicit approval "
        "(Task 2). Remove this xfail marker once fixed."
    ),
)
def test_included_combo_components_are_not_extracted_as_standalone_items():
    extracted_names = {item.name for item, _qty, _mod in _extract_items_from_message(
        _BOT_SUMMARY_TEXT, _CATALOG
    )}

    # Desired behaviour: because the only real catalog match in this text is
    # the included-components description of a combo the customer never
    # named by its real add_items action, extraction should find NOTHING
    # usable here — not a fragment of "Chips" and "Buddy 440ml" standing in
    # for a R119.90 combo plus a hallucinated R99.90 item that doesn't exist
    # in the catalog at all.
    assert extracted_names == set(), (
        f"Expected no standalone items extracted from combo-included-component "
        f"prose, got {extracted_names!r} — this is the exact BO-000004 mechanism: "
        f"included components silently become the entire committed order."
    )


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
_COLLISION_CATALOG = [
    _FakeItem(10, "Smash Burger Combo", 12000),
    _FakeItem(11, "Sprite", 2000),
]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Deterministic extractor double-adds when an included-drink option "
        "value collides with a real standalone menu item's name — e.g. on "
        "the real Mochachos catalog, choosing 'Buddy' as a combo's included "
        "drink would collide with the standalone 'Buddy 440ml' item the "
        "same way. Fix pending explicit approval (Task 2). Remove this "
        "xfail marker once fixed."
    ),
)
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
