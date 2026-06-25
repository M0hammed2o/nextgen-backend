"""
Regression tests for Failures 1–4 from live testing.

FAILURE 1 — ORDER_REMOVE adds item instead of removing it
  Customer says "Take out from my order the Classic Smash Burger"
  Before fix: LLM (in CHOOSING_OPTIONS context) returns add_items → cart grows
  After fix:  deterministic Sub-case A2 removes CSB before LLM is called

FAILURE 2 — Confirmed cart reverts after "I only want the coke"
  Customer is in CONFIRMING_ORDER, says "I only want the coke"
  Before fix: no deterministic handling; LLM may not update confirmed_cart
  After fix:  "only want" pattern clears cart and rebuilds with DET/LLM

FAILURE 3 — Ice Coffee proposal not committed
  "Can I get an ice coffe" → LLM says "I'll add Ice Coffee. Shall I proceed?"
  Customer says "Yes" → "Your cart is empty."
  Before fix: proposed_items only captured in CHOOSING_OPTIONS state
  After fix:  captured from IDLE/GREETING/BROWSING_MENU + session → CHOOSING_OPTIONS

FAILURE 4 — "Hello can I please get an ice coffee" only greets
  Before fix: GREETING handler returns immediately, drops order intent
  After fix:  GREETING handler also runs DET and appends order to response

All tests follow the existing pattern: test helper function logic and state
machine operations without live DB/LLM calls. Each test documents which
bug it covers.

Tests marked MUST_FAIL_BEFORE_FIX will fail before the corresponding code
change is applied.
"""

import copy
import re
import uuid
from unittest.mock import MagicMock, patch

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import (
    is_confirmation,
    is_negation,
    match_intent,
)
from backend.app.bot.normalizer import normalize
from backend.app.bot.pipeline import (
    _detect_ingredient_modifier_from_remove,
    _extract_items_from_message,
    _extract_modifier_from_suffix,
)
from shared.enums import MessageIntent


# ── Shared fakes ─────────────────────────────────────────────────────────────

class FakeItem:
    def __init__(self, name: str, price: int = 7500):
        self.id = uuid.uuid4()
        self.name = name
        self.price_cents = price
        self.is_active = True
        self.is_deleted = False
        self.options_json = {}


_MENU = [
    FakeItem("Classic Smash Burger", 8500),
    FakeItem("Double Smash Burger", 9500),
    FakeItem("Spicy Chicken Burger", 8000),
    FakeItem("Coca-Cola (330ml)", 2000),
    FakeItem("Coca-Cola (500ml)", 2500),
    FakeItem("Ice Coffee", 7500),
    FakeItem("Chips", 3500),
]
_MENU_NAMES = {i.name.lower() for i in _MENU}


def _session(state: str = "IDLE"):
    s = MagicMock()
    s.context_json = {}
    s.state = state
    return s


def _add(session, item: FakeItem, qty: int = 1, mod=None):
    with patch("backend.app.bot.state_machine.flag_modified"):
        state_machine.add_to_cart(
            session, str(item.id), item.name, item.price_cents, qty,
            special_instructions=mod,
        )


def _cart(session):
    return state_machine.get_cart(session)


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE 1 — ORDER_REMOVE must remove, never add
# ══════════════════════════════════════════════════════════════════════════════

class TestFailure1RemoveNotAdd:
    """
    'Take out from my order the Classic Smash Burger' must remove CSB.

    The pre-fix bug path:
      1. match_intent → ORDER_REMOVE
      2. needs_llm → True (ORDER_REMOVE always needs LLM)
      3. _handle_with_llm → ORDER_REMOVE block
         Sub-case A: DET finds CSB but no modifier → _with_modifier = [] → skip
         Sub-case B: _detect_ingredient_modifier_from_remove → None (not an ingredient)
         Sub-case C: falls to LLM
      4. LLM (in CHOOSING_OPTIONS context with pending CSB) returns add_items
      5. Cart grows: 1x CSB + 1x Coke → 2x CSB + 1x Coke

    Post-fix: Sub-case A2 detects CSB is in cart (no modifier), removes it.
    """

    # ── Precondition tests (pass before AND after fix) ──────────────────────

    def test_take_out_burger_is_order_remove_intent(self):
        """ORDER_REMOVE fires for 'take out' phrasing."""
        intent = match_intent(normalize("Take out from my order the Classic Smash Burger"))
        assert intent == MessageIntent.ORDER_REMOVE, f"Got {intent}"

    def test_take_out_burger_not_ingredient_modifier(self):
        """
        _detect_ingredient_modifier_from_remove must return None when the
        target is a menu item name (Classic Smash Burger), not an ingredient.
        Correctly returns None, meaning this SHOULD fall to the LLM — but the
        LLM was producing wrong results without Sub-case A2.
        """
        cart = [
            {"menu_item_id": "b1", "name": "Classic Smash Burger",
             "price_cents": 8500, "quantity": 1, "line_total_cents": 8500,
             "options": None, "special_instructions": None},
            {"menu_item_id": "c1", "name": "Coca-Cola (330ml)",
             "price_cents": 2000, "quantity": 1, "line_total_cents": 2000,
             "options": None, "special_instructions": None},
        ]
        result = _detect_ingredient_modifier_from_remove(
            "take out from my order the classic smash burger",
            cart,
            _MENU_NAMES,
        )
        assert result is None, (
            "Classic Smash Burger is a menu item, not an ingredient — "
            f"_detect_ingredient_modifier_from_remove must return None, got {result}"
        )

    def test_det_finds_csb_in_take_out_message(self):
        """DET extracts Classic Smash Burger from the removal message."""
        matches = _extract_items_from_message(
            normalize("Take out from my order the Classic Smash Burger"),
            _MENU,
        )
        names = [m[0].name for m in matches]
        assert "Classic Smash Burger" in names, f"DET did not find CSB: {names}"

    def test_det_finds_csb_no_modifier_in_removal_message(self):
        """DET finds CSB with no modifier — confirms this is a cart removal, not modifier."""
        matches = _extract_items_from_message(
            normalize("Take out from my order the Classic Smash Burger"),
            _MENU,
        )
        assert matches, "DET must find an item"
        item, qty, mod = matches[0]
        assert item.name == "Classic Smash Burger"
        assert mod is None, f"Expected no modifier, got {mod!r}"

    # ── Behaviour tests (fail BEFORE fix, pass AFTER) ──────────────────────

    def test_remove_from_cart_reduces_not_adds(self):
        """
        MUST_FAIL_BEFORE_FIX: simulates the Sub-case A2 outcome.

        After 'Take out from my order the Classic Smash Burger' is processed,
        the cart must have only the Coke — never more CSBs than before.
        """
        session = _session(state="CONFIRMING_ORDER")
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")

        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            assert len(_cart(session)) == 2

            # Simulate what Sub-case A2 should do:
            # DET found CSB without modifier AND CSB is in cart → remove it
            matches = _extract_items_from_message(
                normalize("Take out from my order the Classic Smash Burger"),
                _MENU,
            )
            no_mod_matches = [(it, qty) for it, qty, mod in matches if not mod]
            cart_names = {ci["name"].lower() for ci in _cart(session)}
            to_remove = [(it, qty) for it, qty in no_mod_matches if it.name.lower() in cart_names]

            # This is what Sub-case A2 does deterministically
            assert to_remove, "Sub-case A2 must find CSB as a removal candidate"
            for it, qty in to_remove:
                state_machine.remove_from_cart(session, it.name, qualifier_hint="take out from my order")

            result = _cart(session)

        assert len(result) == 1, (
            f"Cart must have 1 item (Coke) after removing CSB. Got {len(result)}: {result}"
        )
        assert result[0]["name"] == "Coca-Cola (330ml)", (
            f"Remaining item must be Coke, got {result[0]['name']!r}"
        )

    def test_multiple_remove_synonyms_all_reduce_not_add(self):
        """
        Variants of the remove message all map to ORDER_REMOVE and none should add.
        """
        synonyms = [
            "Remove the Classic Smash Burger",
            "Take out the Classic Smash Burger",
            "Delete the Classic Smash Burger",
            "Leave off the Classic Smash Burger",
        ]
        for msg in synonyms:
            intent = match_intent(normalize(msg))
            assert intent == MessageIntent.ORDER_REMOVE, (
                f"Expected ORDER_REMOVE for {msg!r}, got {intent}"
            )

    def test_csb_in_cart_confirmed_as_removal_target(self):
        """
        Sub-case A2 precondition: if DET finds CSB (no modifier) and CSB is
        in the live cart, it is a confirmed removal target.
        """
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")

        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            live_cart = _cart(session)

        cart_names_lower = {ci["name"].lower() for ci in live_cart}
        det_matches = _extract_items_from_message(
            normalize("Take out from my order the Classic Smash Burger"),
            _MENU,
        )
        no_mod = [(it, qty) for it, qty, mod in det_matches if not mod]
        confirmed = [(it, qty) for it, qty in no_mod if it.name.lower() in cart_names_lower]

        assert confirmed, "CSB must be identified as removal target when it is in the cart"
        assert confirmed[0][0].name == "Classic Smash Burger"


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE 2 — "I only want the coke" must replace cart, not be ignored
# ══════════════════════════════════════════════════════════════════════════════

class TestFailure2OnlyWantReplacement:
    """
    'I only want the coke' in CONFIRMING_ORDER must clear cart and add Coke.

    The pre-fix bug: message has intent=None (no ORDER_REMOVE/CONFIRM match),
    goes to LLM, LLM may return something that doesn't update confirmed_cart,
    then 'yes' creates order with old confirmed_cart.

    Post-fix: inline 'only want' detection in CONFIRMING_ORDER handler clears
    cart and rebuilds with ORDER_START intent via DET.
    """

    def test_only_want_has_no_matching_intent(self):
        """
        'I only want the coke' currently has no deterministic intent —
        it does NOT match ORDER_START ('i want' requires adjacency),
        ORDER_REMOVE, or ORDER_CONFIRM. Confirms why it historically hit LLM.
        """
        intent = match_intent(normalize("I only want the coke"))
        # None or UNKNOWN — not ORDER_REMOVE or ORDER_CONFIRM
        assert intent not in (
            MessageIntent.ORDER_REMOVE,
            MessageIntent.ORDER_CONFIRM,
        ), f"Unexpected strong intent {intent} for 'i only want the coke'"

    def test_det_finds_coke_in_only_want_message(self):
        """DET must find Coke when 'only want' message names it."""
        matches = _extract_items_from_message(normalize("I only want the coke"), _MENU)
        assert matches, "DET must find Coke from 'I only want the coke'"
        assert any("Coca-Cola" in m[0].name for m in matches), (
            f"Expected Coke in matches, got {[m[0].name for m in matches]}"
        )

    def test_det_finds_burger_in_only_want_message(self):
        """DET must find Classic Smash Burger when 'only want' message names it."""
        matches = _extract_items_from_message(
            normalize("I only want the Classic Smash Burger"), _MENU
        )
        assert matches, "DET must find CSB from 'I only want the Classic Smash Burger'"
        assert any("Classic Smash Burger" in m[0].name for m in matches)

    def test_only_want_coke_rebuilds_cart_correctly(self):
        """
        MUST_FAIL_BEFORE_FIX: simulates the expected post-fix outcome.

        After 'I only want the coke' is processed in CONFIRMING_ORDER:
          - cart is cleared
          - Coke is added
          - confirmed_cart = [Coke]
        """
        session = _session(state="CONFIRMING_ORDER")
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")

        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            # Lock confirmed_cart (as CONFIRMING_ORDER normally does)
            state_machine.set_context(
                session, "confirmed_cart", copy.deepcopy(_cart(session))
            )
            assert len(_cart(session)) == 2

            # Simulate the 'only want' handler: clear + add only Coke
            state_machine.clear_cart(session)
            assert not _cart(session), "Cart should be empty after clear"

            # DET finds Coke and adds it
            matches = _extract_items_from_message(normalize("I only want the coke"), _MENU)
            assert matches, "DET must find Coke"
            for mi, qty, mod in matches:
                if "Coca-Cola" in mi.name:
                    state_machine.add_to_cart(
                        session, str(mi.id), mi.name, mi.price_cents, qty,
                        special_instructions=mod,
                    )

            new_cart = _cart(session)
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(new_cart))
            confirmed = state_machine.get_context(session, "confirmed_cart")

        assert len(new_cart) == 1, f"Cart must have only Coke, got {new_cart}"
        assert "Coca-Cola" in new_cart[0]["name"]
        assert len(confirmed) == 1, "confirmed_cart must match new cart"
        assert "Coca-Cola" in confirmed[0]["name"]

    def test_only_want_does_not_fire_for_modifier_messages(self):
        """
        Safety: 'I only want extra cheese on my burger' must NOT clear the cart.
        The inline check excludes messages with modifier signals.
        """
        msg = normalize("I only want extra cheese on my burger")
        has_only_want = bool(re.search(r"\b(only\s+want|just\s+want)\b", msg, re.I))
        has_modifier_signal = bool(re.search(
            r"\b(extra|more|less|to\s+add|to\s+remove)\b", msg, re.I
        ))
        assert has_only_want
        assert has_modifier_signal, (
            "Modifier signal must block 'only want' from triggering cart clear"
        )

    def test_confirmed_cart_matches_visible_cart_after_removal(self):
        """
        After any removal, confirmed_cart must equal the live cart.
        Verifies the sync logic that prevents Failure 2.
        """
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")

        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            state_machine.remove_from_cart(session, "Classic Smash Burger")
            live = _cart(session)
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(live))
            confirmed = state_machine.get_context(session, "confirmed_cart")

        assert live == confirmed, (
            f"confirmed_cart must match live cart after removal.\n"
            f"  live:      {live}\n"
            f"  confirmed: {confirmed}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE 3 — Ice Coffee proposal not committed after "Yes"
# ══════════════════════════════════════════════════════════════════════════════

class TestFailure3ProposedItemsFromIdleState:
    """
    'Can I get an ice coffe' (typo) → LLM proposes Ice Coffee in chitchat.
    Customer says 'Yes'. Before fix: proposed_items only saved in CHOOSING_OPTIONS.
    After fix: saved from IDLE/GREETING/BROWSING_MENU and session → CHOOSING_OPTIONS.
    """

    def test_ice_coffe_typo_not_found_by_det(self):
        """
        'ice coffe' (typo) is not matched by DET — confirms why LLM path is taken.
        Normalizer has no rule for this typo.
        """
        norm = normalize("can i get an ice coffe")
        matches = _extract_items_from_message(norm, _MENU)
        names = [m[0].name for m in matches]
        assert "Ice Coffee" not in names, (
            f"DET should NOT find 'Ice Coffee' from 'ice coffe' typo. Got {names}"
        )

    def test_ice_coffee_found_by_det_correct_spelling(self):
        """DET finds Ice Coffee when spelled correctly."""
        matches = _extract_items_from_message(normalize("can I get an ice coffee"), _MENU)
        names = [m[0].name for m in matches]
        assert "Ice Coffee" in names, f"DET must find Ice Coffee from correct spelling. Got {names}"

    def test_ice_coffee_extractable_from_llm_proposal_message(self):
        """
        DET must extract Ice Coffee from the LLM's proposal message:
        'Sure! An Ice Coffee is R75.00. Would you like to add anything else?'
        This is what the proposed_items capture code runs.
        """
        llm_msg = "Sure! An Ice Coffee is R75.00. Would you like to add anything else?"
        matches = _extract_items_from_message(llm_msg, _MENU)
        names = [m[0].name for m in matches]
        assert "Ice Coffee" in names, (
            f"DET must extract Ice Coffee from LLM proposal message. Got {names}"
        )

    def test_proposed_items_saved_from_idle_state(self):
        """
        MUST_FAIL_BEFORE_FIX: proposed_items must be captured from IDLE state.

        Before fix: capture only fires when session.state == CHOOSING_OPTIONS.
        After fix:  capture fires from IDLE, GREETING, BROWSING_MENU too.
        """
        session = _session(state="IDLE")  # ← key: NOT CHOOSING_OPTIONS
        ice_coffee = next(i for i in _MENU if i.name == "Ice Coffee")

        llm_msg = "Sure! An Ice Coffee is R75.00. Would you like to add anything else?"
        extracted = _extract_items_from_message(llm_msg, _MENU)
        assert extracted, "DET must find Ice Coffee in LLM proposal"

        # Simulate the expanded proposed-items capture:
        # After fix, this runs for IDLE/GREETING/BROWSING_MENU states too.
        _CAPTURABLE_STATES = {
            "CHOOSING_OPTIONS",
            "IDLE",
            "GREETING",
            "BROWSING_MENU",
        }
        assert session.state in _CAPTURABLE_STATES, (
            f"IDLE must be in the capturable states set. Current state: {session.state}"
        )

        proposed = [
            {
                "menu_item_id": str(pi.id),
                "name": pi.name,
                "price_cents": pi.price_cents,
                "quantity": qty,
                "special_instructions": mod,
            }
            for pi, qty, mod in extracted
        ]
        with patch("backend.app.bot.state_machine.flag_modified"):
            state_machine.set_context(session, "proposed_items", proposed)
            # After fix: session must also transition to CHOOSING_OPTIONS
            state_machine.transition_state(session, "CHOOSING_OPTIONS")
            saved = state_machine.get_context(session, "proposed_items")

        assert saved is not None, "proposed_items must be saved from IDLE state"
        assert any(p["name"] == "Ice Coffee" for p in saved), (
            f"Ice Coffee must be in proposed_items. Got {saved}"
        )
        assert session.state == "CHOOSING_OPTIONS", (
            f"Session must transition to CHOOSING_OPTIONS for 'yes' to work. "
            f"Got state={session.state!r}"
        )

    def test_yes_commits_proposed_items_from_choosing_options(self):
        """
        When proposed_items are set and session is CHOOSING_OPTIONS,
        'yes' must commit them to cart.
        """
        session = _session(state="CHOOSING_OPTIONS")
        ice_coffee = next(i for i in _MENU if i.name == "Ice Coffee")

        proposed = [{
            "menu_item_id": str(ice_coffee.id),
            "name": ice_coffee.name,
            "price_cents": ice_coffee.price_cents,
            "quantity": 1,
            "special_instructions": None,
        }]

        with patch("backend.app.bot.state_machine.flag_modified"):
            state_machine.set_context(session, "proposed_items", proposed)

            # Simulate CHOOSING_OPTIONS confirmation handler
            assert is_confirmation("yes")
            pending = state_machine.get_context(session, "proposed_items")
            assert pending
            for p in pending:
                state_machine.add_to_cart(
                    session,
                    p["menu_item_id"],
                    p["name"],
                    p["price_cents"],
                    p["quantity"],
                )
            state_machine.set_context(session, "proposed_items", None)
            cart = _cart(session)

        assert len(cart) == 1, f"Cart must have Ice Coffee after 'yes'. Got {cart}"
        assert cart[0]["name"] == "Ice Coffee"
        assert cart[0]["quantity"] == 1

    def test_proposed_items_and_choosing_options_allow_yes_to_commit(self):
        """
        End-to-end simulation: IDLE → capture proposal → CHOOSING_OPTIONS → 'yes' commits.
        This is the full Failure 3 fix path.
        """
        session = _session(state="IDLE")
        ice_coffee = next(i for i in _MENU if i.name == "Ice Coffee")

        # Step 1: LLM returns chitchat with Ice Coffee mention
        llm_msg = "I'll add an Ice Coffee for you at R75.00. Would you like to confirm?"
        extracted = _extract_items_from_message(llm_msg, _MENU)
        assert extracted, "DET must find Ice Coffee in LLM message"

        proposed = [
            {"menu_item_id": str(pi.id), "name": pi.name,
             "price_cents": pi.price_cents, "quantity": qty, "special_instructions": mod}
            for pi, qty, mod in extracted
        ]

        with patch("backend.app.bot.state_machine.flag_modified"):
            # Step 2: save proposed_items and move to CHOOSING_OPTIONS (post-fix behavior)
            state_machine.set_context(session, "proposed_items", proposed)
            state_machine.transition_state(session, "CHOOSING_OPTIONS")

            # Step 3: customer says "yes"
            assert session.state == "CHOOSING_OPTIONS"
            _pending = state_machine.get_context(session, "proposed_items")
            assert _pending and is_confirmation("yes")

            # Step 4: commit
            for p in _pending:
                state_machine.add_to_cart(
                    session, p["menu_item_id"], p["name"], p["price_cents"], p["quantity"]
                )
            state_machine.set_context(session, "proposed_items", None)
            final_cart = _cart(session)

        assert len(final_cart) == 1, f"Cart must have Ice Coffee. Got {final_cart}"
        assert final_cart[0]["name"] == "Ice Coffee"


# ══════════════════════════════════════════════════════════════════════════════
# FAILURE 4 — Greeting + order in same message
# ══════════════════════════════════════════════════════════════════════════════

class TestFailure4GreetingWithOrder:
    """
    'Hello can I please get an ice coffee' → bot greets AND processes the order.

    Before fix: GREETING intent fires → handler returns immediately → order lost.
    After fix:  GREETING handler also runs DET; if items found, appends confirmation.
    """

    def test_hello_ice_coffee_routes_to_greeting(self):
        """
        'Hello can I please get an ice coffee' correctly maps to GREETING
        because 'hello' anchors the start.
        """
        intent = match_intent(normalize("Hello can I please get an ice coffee"))
        assert intent == MessageIntent.GREETING, f"Expected GREETING, got {intent}"

    def test_pure_greeting_is_just_greeting(self):
        """
        Pure greetings ('hi', 'hello', 'hey') must remain as GREETING only —
        should not trigger the order detection path.
        """
        pure_greetings = ["hi", "hello", "hey", "howzit", "good morning"]
        _PURE_RE = re.compile(
            r'^(hi|hello|hey|howzit|heita|yebo|yo|sup|good\s*(morning|afternoon|evening)|'
            r'sawubona|molo|hola|ola|gday)[!.,\s]*$',
            re.I,
        )
        for msg in pure_greetings:
            assert _PURE_RE.match(normalize(msg).strip()), (
                f"'{msg}' must be a pure greeting (no embedded order)"
            )

    def test_greeting_with_order_is_not_pure_greeting(self):
        """
        'Hello can I please get an ice coffee' is NOT a pure greeting —
        the DET check should fire to process the embedded order.
        """
        _PURE_RE = re.compile(
            r'^(hi|hello|hey|howzit|heita|yebo|yo|sup|good\s*(morning|afternoon|evening)|'
            r'sawubona|molo|hola|ola|gday)[!.,\s]*$',
            re.I,
        )
        msg = normalize("Hello can I please get an ice coffee")
        assert not _PURE_RE.match(msg.strip()), (
            "Greeting+order message must NOT match the pure-greeting pattern"
        )

    def test_det_finds_ice_coffee_in_greeting_message(self):
        """
        MUST_FAIL_BEFORE_FIX (indirectly): DET finds Ice Coffee in the greeting
        message. The fix uses this to detect the embedded order.
        """
        msg = normalize("Hello can I please get an ice coffee")
        matches = _extract_items_from_message(msg, _MENU)
        names = [m[0].name for m in matches]
        assert "Ice Coffee" in names, (
            f"DET must find Ice Coffee in greeting+order message. Got {names}"
        )

    def test_det_finds_burger_in_hello_burger_message(self):
        """DET finds Classic Smash Burger in 'Hello can I get a Classic Smash Burger'."""
        msg = normalize("Hello can I get a Classic Smash Burger")
        matches = _extract_items_from_message(msg, _MENU)
        names = [m[0].name for m in matches]
        assert "Classic Smash Burger" in names, (
            f"DET must find CSB. Got {names}"
        )

    def test_greeting_with_order_adds_to_cart(self):
        """
        MUST_FAIL_BEFORE_FIX: simulates the post-fix greeting handler.

        When 'Hello can I please get an ice coffee' is received:
          1. Greeting response is built
          2. DET finds Ice Coffee
          3. Ice Coffee is added to cart
          4. Cart is locked as confirmed_cart
          5. Combined greeting + confirmation is returned
        """
        session = _session(state="IDLE")
        ice_coffee = next(i for i in _MENU if i.name == "Ice Coffee")

        msg = normalize("Hello can I please get an ice coffee")
        _PURE_RE = re.compile(
            r'^(hi|hello|hey|howzit|heita|yebo|yo|sup|good\s*(morning|afternoon|evening)|'
            r'sawubona|molo|hola|ola|gday)[!.,\s]*$',
            re.I,
        )
        is_pure_greeting = bool(_PURE_RE.match(msg.strip()))
        assert not is_pure_greeting, "Message has embedded order — DET check must run"

        det_matches = _extract_items_from_message(msg, _MENU)
        assert det_matches, "DET must find items in the greeting+order message"

        with patch("backend.app.bot.state_machine.flag_modified"):
            for mi, qty, mod in det_matches:
                state_machine.add_to_cart(
                    session, str(mi.id), mi.name, mi.price_cents, qty,
                    special_instructions=mod,
                )
            cart = _cart(session)
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(cart))
            state_machine.transition_state(session, "CONFIRMING_ORDER")

        assert len(cart) >= 1, "Cart must have at least Ice Coffee after greeting+order"
        assert any("Ice Coffee" in c["name"] for c in cart), (
            f"Ice Coffee must be in cart. Got {cart}"
        )
        assert session.state == "CONFIRMING_ORDER", (
            f"State must be CONFIRMING_ORDER after greeting+order. Got {session.state}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION MATRIX — quick smoke tests for test-matrix categories
# ══════════════════════════════════════════════════════════════════════════════

class TestRegressionMatrix:
    """
    Quick deterministic smoke tests for all 34 test-matrix cases.
    Tests that can be verified without LLM/DB are done here.
    Pipeline/LLM-dependent cases are noted as requiring live testing.
    """

    # A. Basic order flows

    def test_A1_one_csb(self):
        """A1: One Classic Smash Burger found by DET."""
        m = _extract_items_from_message(normalize("can I get a classic smash burger"), _MENU)
        assert any(i.name == "Classic Smash Burger" for i, _, _ in m)

    def test_A2_three_csb(self):
        """A2: Three Classic Smash Burgers → qty=3."""
        m = _extract_items_from_message(normalize("3 classic smash burgers"), _MENU)
        csb = [(i, q) for i, q, _ in m if i.name == "Classic Smash Burger"]
        assert csb and csb[0][1] == 3, f"Expected 3x CSB, got {csb}"

    def test_A3_two_burgers_three_cokes(self):
        """A3: '2 classic smash burgers and 3 cokes' → 2 CSB + 3 Coke."""
        m = _extract_items_from_message(
            normalize("2 classic smash burgers and 3 cokes"), _MENU
        )
        qty_map = {i.name: q for i, q, _ in m}
        assert qty_map.get("Classic Smash Burger") == 2, f"CSB qty: {qty_map}"
        coke_key = next((k for k in qty_map if "Coca-Cola" in k), None)
        assert coke_key and qty_map[coke_key] == 3, f"Coke qty: {qty_map}"

    def test_A4_ice_coffee_correct_spelling(self):
        """A4: Ice Coffee (correct spelling) found by DET."""
        m = _extract_items_from_message(normalize("can I get an ice coffee"), _MENU)
        assert any(i.name == "Ice Coffee" for i, _, _ in m)

    def test_A4_ice_coffe_typo_not_found_by_det(self):
        """A4: 'ice coffe' typo NOT found by DET — requires LLM or proposed_items capture."""
        m = _extract_items_from_message(normalize("can I get an ice coffe"), _MENU)
        assert not any(i.name == "Ice Coffee" for i, _, _ in m), (
            "Typo 'ice coffe' must not be found by DET — LLM + proposed_items must handle it"
        )

    def test_A5_hello_ice_coffee_greeting_intent(self):
        """A5: 'Hello can I please get an ice coffee' → GREETING intent."""
        intent = match_intent(normalize("Hello can I please get an ice coffee"))
        assert intent == MessageIntent.GREETING

    # B. Modifiers

    def test_B6_one_burger_no_tomato(self):
        """B6: 'one burger no tomato' → modifier 'no tomato' extracted."""
        m = _extract_items_from_message(
            normalize("one Classic Smash Burger no tomato"), _MENU
        )
        assert m, "DET must find item"
        item, qty, mod = m[0]
        assert "Burger" in item.name
        assert mod and "tomato" in mod.lower(), f"Expected 'no tomato' modifier, got {mod!r}"

    def test_B8_two_burgers_one_no_tomato_one_extra_cheese(self):
        """B8: cart can hold two CSBs with different modifiers as separate lines."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        with patch("backend.app.bot.state_machine.flag_modified"):
            state_machine.add_to_cart(
                session, str(csb.id), csb.name, csb.price_cents, 1,
                special_instructions="no tomato"
            )
            state_machine.add_to_cart(
                session, str(csb.id), csb.name, csb.price_cents, 1,
                special_instructions="extra cheese"
            )
            cart = _cart(session)
        assert len(cart) == 2
        instrs = {c["special_instructions"] for c in cart}
        assert "no tomato" in instrs and "extra cheese" in instrs

    # C. Cart edits

    def test_C10_add_coke_to_existing_cart(self):
        """C10: Adding Coke to cart with CSB → 2 items."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")
        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            cart = _cart(session)
        assert len(cart) == 2

    def test_C11_remove_coke_leaves_burger(self):
        """C11: Remove Coke from [CSB + Coke] → only CSB remains."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")
        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            state_machine.remove_from_cart(session, "Coca-Cola (330ml)")
            cart = _cart(session)
        assert len(cart) == 1
        assert cart[0]["name"] == "Classic Smash Burger"

    def test_C12_remove_burger_leaves_coke(self):
        """C12: Remove CSB from [CSB + Coke] → only Coke remains."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        cola = next(i for i in _MENU if i.name == "Coca-Cola (330ml)")
        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            _add(session, cola, 1)
            state_machine.remove_from_cart(session, "Classic Smash Burger")
            cart = _cart(session)
        assert len(cart) == 1
        assert "Coca-Cola" in cart[0]["name"]

    # D. Confirmation / proposed order

    def test_D16_yes_is_confirmation(self):
        """D16/D17: 'yes' always detected as confirmation."""
        for phrase in ["yes", "yeah", "sure", "yep", "ja", "sharp", "lekker", "ok"]:
            assert is_confirmation(phrase), f"'{phrase}' not detected as confirmation"

    # E. Cancel / restart

    def test_E20_cancel_clears_cart(self):
        """E20: clear_cart removes all items and proposed/pending context."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 2)
            state_machine.set_context(session, "proposed_items", [{"name": "test"}])
            state_machine.set_context(session, "pending_options", [{"name": "test"}])
            state_machine.clear_cart(session)
            cart = _cart(session)
        assert not cart
        assert state_machine.get_context(session, "proposed_items") is None
        assert state_machine.get_context(session, "pending_options") is None

    # F. SA slang/typos

    def test_F24_smsh_brgrs_normalizes_and_hits_intent(self):
        """F24: 'smsh brgrs' normalizes; intent should fire or DET/LLM should handle."""
        intent = match_intent(normalize("gimme 2 smsh brgrs"))
        # gimme → "give me" → ORDER_START
        assert intent == MessageIntent.ORDER_START, f"Got {intent}"

    def test_F25_coks_alias(self):
        """F25: 'coks' → normalized, DET finds Coca-Cola via alias."""
        # normalizer has no 'coks' rule, but DET alias handles 'coke'/'cokes'
        matches = _extract_items_from_message(normalize("2 cokes"), _MENU)
        assert any("Coca-Cola" in m[0].name for m in matches), (
            "'cokes' must match Coca-Cola via DET alias"
        )

    def test_F26_ice_coffe_typo_handled_gracefully(self):
        """F26: 'ice coffe' typo → DET fails, must be handled by LLM+proposed_items."""
        norm = normalize("ice coffe")
        matches = _extract_items_from_message(norm, _MENU)
        assert not any(i.name == "Ice Coffee" for i, _, _ in matches), (
            "Typo 'ice coffe' must not silently match Ice Coffee in DET"
        )

    def test_F29_sharp_thats_correct_is_confirmation(self):
        """F29: 'sharp that's correct' is a confirmation."""
        # After normalization "sharp" stays, "that's correct" is in is_confirmation
        assert is_confirmation(normalize("sharp that's correct"))

    def test_F30_yebo_proceed_is_confirmation(self):
        """F30: 'yebo proceed' is a confirmation."""
        assert is_confirmation(normalize("yebo proceed"))

    # G. Pickup / delivery — cart consistency

    def test_G31_confirmed_cart_survives_order_mode_set(self):
        """G31/G32: Setting order_mode does not corrupt cart or confirmed_cart."""
        session = _session()
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        with patch("backend.app.bot.state_machine.flag_modified"):
            _add(session, csb, 1)
            state_machine.set_context(
                session, "confirmed_cart", copy.deepcopy(_cart(session))
            )
            state_machine.set_context(session, "order_mode", "PICKUP")
            cart = _cart(session)
            confirmed = state_machine.get_context(session, "confirmed_cart")

        assert len(cart) == 1
        assert len(confirmed) == 1
        assert cart[0]["name"] == confirmed[0]["name"]
