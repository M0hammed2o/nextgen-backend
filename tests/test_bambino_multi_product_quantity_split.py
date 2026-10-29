"""
Phase 1 investigation (incident, 2026-10-27): "15 Bambino Meals" silently
omitted from an order while a sibling item ("3 Chicken Schwarma") was added
correctly, plus a report that one reply incorrectly asked the customer to
choose between "Half Chicken" and "Full Chicken" -- items nobody mentioned.

Ported verbatim (2026-10-27) from the original investigation branch (cut from
`origin/main` @ 551b51f) onto this branch, cut from `ad7f486` -- the actually
currently-deployed commit (tip of `fix/whatsapp-ordering-reliability`),
confirmed by the user independently against the live Render service. Rerun
here to confirm the same 3 failures reproduce against the real deployed code,
not just against `main`. See KNOWN_BUGS.md / WORKLOG.md for the full
root-cause writeup (BUG-0172).

1. test_bambino_meals_generic_phrase_is_not_silently_dropped
   `_extract_items_from_message` only matches a menu item when the item's
   FULL catalog name (e.g. "Bambino Meal: Jose") appears verbatim in the
   message. A plain "Bambino Meals" (no specific name) never matches any of
   the 4 real "Bambino Meal: <name>" products, so it is dropped with zero
   trace -- while "chicken Schwarma" in the SAME message matches fine in a
   later chunk, so the overall extraction is non-empty and the deterministic
   caller (pipeline.py line ~3165) commits just the Schwarma, never
   consulting the LLM for the dropped remainder.

2. test_generic_chicken_mention_incorrectly_triggers_half_full_ambiguity
   `_find_size_variants` strips SIZE_KEYWORDS ("half", "full", "small", ...)
   from item names to group size variants. "Half Chicken" and "Full Chicken"
   strip down to the shared base "chicken", and the match-ratio check only
   requires the single significant base word ("chicken") to appear anywhere
   in the message. ANY mention of a dish containing the bare word "chicken"
   (e.g. "chicken Schwarma", "chicken burrito") -- with no small/medium/large
   word already present -- satisfies this and is capable of producing an
   unsolicited "Which Chicken would you like? Half Chicken / Full Chicken"
   question that has nothing to do with what was actually ordered.

3. test_bambino_meals_alone_extracts_nothing_deterministically
   A bare "Give me one Bambino Meals" (no sibling item in the message)
   produces ZERO deterministic matches at all, confirming the request falls
   through entirely to the LLM with no deterministic signal that the menu
   actually has 4 matching products -- a likely contributor to the LLM
   inventing an unrelated clarification instead of asking about the real
   Bambino variants. (Not independently reproducible without a live,
   authorised OpenAI key -- see live_model_eval harness status in WORKLOG.md.)

UPDATED 2026-10-27, after the Phase 2 fix, per explicit instruction to
"retain the original regression evidence" but "assert the correct public
behaviour at the appropriate boundary": tests 1 and 3 kept their ORIGINAL
assertion against `_extract_items_from_message` UNCHANGED, in full, as a
pinned characterisation of that function's own, still-unchanged, low-level
behaviour (it was never meant to be the fix -- see BUG-0172's Phase 2 entry
in KNOWN_BUGS.md) -- that assertion is no longer the test's pass/fail
criterion by itself. Each test then ADDS an assertion against the real fix,
`_find_named_variant_family` (the new pre-check that runs BEFORE this
function at the actual call site, `_handle_with_llm`), which is the correct
boundary for "is this generic phrase recognised." Nothing was weakened:
every original assertion is still present and still describes real,
verified behaviour; the test as a whole now also verifies the customer-
facing fix, and passes. Test 2 is unchanged -- it already passes, since
Problem B's fix lives directly inside `_find_size_variants`, the exact
function it tests.

Catalog below mirrors the real Mochachos menu (scripts/seed_mochachos_demo.py)
for the sections involved: "Flame-Grilled Chicken", "Chicken Meals",
"Schwarmas, Burritos and Quesadillas".
"""

from backend.app.bot.pipeline import (
    _extract_items_from_message,
    _find_named_variant_family,
    _find_size_variants,
)


class _FakeItem:
    def __init__(self, id_, name, price_cents, options_json=None):
        self.id = id_
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False
        self.options_json = options_json or {}


_CATALOG = [
    # "Flame-Grilled Chicken" section
    _FakeItem(1, "Quarter Chicken", 5990),
    _FakeItem(2, "Half Chicken", 11490),
    _FakeItem(3, "Full Chicken", 21990),
    # "Chicken Meals" section -- 4 same-priced Bambino variants
    _FakeItem(4, "Bambino Meal: Jose", 8490),
    _FakeItem(5, "Bambino Meal: Pedro", 8490),
    _FakeItem(6, "Bambino Meal: Speedy", 8490),
    _FakeItem(7, "Bambino Meal: Juanita", 8490),
    _FakeItem(8, "2 Chicken Fillets and a Side", 9990),
    # "Schwarmas, Burritos and Quesadillas" section
    _FakeItem(9, "Chicken Schwarma", 9490),
    _FakeItem(10, "Rump Steak Schwarma", 10490),
    _FakeItem(11, "Veggie Schwarma", 9490),
    _FakeItem(12, "Chicken Burrito", 8990),
    _FakeItem(13, "Chicken Quesadilla", 12990),
]

_INCIDENT_MESSAGE = (
    "Ok can you give me 15 Bambino Meals and also give me 3 chicken Schwarma"
)


def test_bambino_meals_generic_phrase_is_not_silently_dropped():
    # Pinned, unchanged, low-level behaviour: the raw substring matcher
    # alone still cannot resolve a generic "Bambino Meals" mention -- it was
    # never meant to be the fix. Only the unambiguous sibling matches here.
    extracted = _extract_items_from_message(_INCIDENT_MESSAGE, _CATALOG)
    names = [item.name for item, _qty, _mod in extracted]
    assert names == ["Chicken Schwarma"], (
        f"this low-level matcher's behaviour is pinned as a known limitation; "
        f"if it ever changes, re-check whether _find_named_variant_family (the "
        f"real fix, asserted below) still needs to run first. Got {names!r}."
    )

    # The REAL fix, at the correct boundary: _find_named_variant_family runs
    # BEFORE _extract_items_from_message at the actual call site
    # (_handle_with_llm) and is what recognises the generic phrase instead
    # of letting it vanish with zero trace.
    family_label, variants = _find_named_variant_family(_INCIDENT_MESSAGE, _CATALOG)
    variant_names = {v.name for v in variants}
    assert family_label == "Bambino Meal", (
        f"expected the Bambino Meal family to be recognised as ambiguous, "
        f"got family_label={family_label!r}"
    )
    assert variant_names == {
        "Bambino Meal: Jose", "Bambino Meal: Pedro",
        "Bambino Meal: Speedy", "Bambino Meal: Juanita",
    }, f"expected all 4 real variants offered, got {variant_names!r}"


def test_generic_chicken_mention_incorrectly_triggers_half_full_ambiguity():
    base_name, variants = _find_size_variants(_INCIDENT_MESSAGE, _CATALOG)
    variant_names = {v.name for v in variants}

    assert not (base_name == "chicken" and variants), (
        f"_find_size_variants treated the bare word 'chicken' (inside "
        f"'chicken Schwarma') as a reference to the Half Chicken / Full "
        f"Chicken size family: base_name={base_name!r}, "
        f"variants={variant_names!r}. This is the deterministic mechanism "
        f"capable of producing an unsolicited 'Did you want Half Chicken or "
        f"Full Chicken?' question on an order that never asked for either."
    )


def test_bambino_meals_alone_extracts_nothing_deterministically():
    # Pinned, unchanged, low-level behaviour: on its own, this matcher still
    # can't resolve a bare family mention with no sibling items to anchor it.
    extracted = _extract_items_from_message("Give me one Bambino Meals", _CATALOG)
    assert extracted == [], (
        f"this low-level matcher's behaviour is pinned as a known limitation; "
        f"the real fix (_find_named_variant_family, asserted below) runs "
        f"before it at the actual call site. Got {extracted!r}."
    )

    # The REAL fix: _find_named_variant_family recognises the family even
    # with no sibling item in the message, so the customer is asked which
    # variant instead of the request silently falling through to the LLM
    # with zero deterministic signal that the menu has 4 matching products.
    family_label, variants = _find_named_variant_family("Give me one Bambino Meals", _CATALOG)
    assert family_label == "Bambino Meal", (
        f"expected the family to be recognised even alone, got {family_label!r}"
    )
    assert len(variants) == 4, f"expected all 4 variants, got {len(variants)}"


# ── Independent review follow-up (2026-10-27), item 5: colons in unrelated
# categories must never be misread as a named-variant family ────────────────
_COLON_COLLISION_CATALOG = _CATALOG + [
    # A SINGLE item using ": " in its name, in a completely different
    # category, with no sibling sharing its label -- must never be treated
    # as an ambiguous family (requires >= 2 variants by construction).
    _FakeItem(14, "Combo Deal: Weekday Special", 9990),
    # A single colon-named dessert, same reasoning, different label again --
    # also checks that two DIFFERENT singleton labels never get merged
    # together under the same family.
    _FakeItem(15, "Dessert Special: Malva Pudding", 4990),
]


def test_single_colon_item_in_unrelated_category_is_never_treated_as_a_family():
    family_label, variants = _find_named_variant_family(
        "I'll have the Combo Deal: Weekday Special please", _COLON_COLLISION_CATALOG,
    )
    assert family_label is None and not variants, (
        f"a single item with no sibling sharing its label must never be "
        f"treated as an ambiguous family, got family_label={family_label!r}, "
        f"variants={[v.name for v in variants]!r}"
    )


def test_generic_mention_of_a_singleton_colon_label_matches_nothing():
    # "Combo Deal" alone (no sibling) must not trigger any clarification --
    # confirms the >= 2 variants requirement actually gates this correctly,
    # not just the exact-name case above.
    family_label, variants = _find_named_variant_family("Can I get a Combo Deal", _COLON_COLLISION_CATALOG)
    assert family_label is None and not variants

    family_label2, variants2 = _find_named_variant_family("Dessert Special please", _COLON_COLLISION_CATALOG)
    assert family_label2 is None and not variants2


def test_two_different_singleton_colon_labels_are_never_merged_into_one_family():
    # Both "Combo Deal: ..." and "Dessert Special: ..." exist, each alone --
    # confirms grouping is keyed strictly by the label text, never merging
    # unrelated labels just because both happen to use ": " in their name.
    family_label, variants = _find_named_variant_family(
        "Combo Deal and Dessert Special please", _COLON_COLLISION_CATALOG,
    )
    assert family_label is None and not variants
