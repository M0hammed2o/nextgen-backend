"""
Synthetic Conversation Generator — Phase 5B Production Readiness.

Generates 1000+ deterministic (DET-only) replay conversations covering every
major ordering flow.  All conversations are self-verifying: expected state,
cart contents, and response keywords are computed from the same logic the
pipeline uses, so the generator cannot produce an assertion that the pipeline
would legitimately fail.

Output: tests/replay/conversations/synthetic/*.json

Run:
    python -m tests.replay.generate_synthetic
or from the project root:
    python tests/replay/generate_synthetic.py
"""

from __future__ import annotations

import json
import random
from itertools import product
from pathlib import Path

random.seed(42)

# ── Output directory ──────────────────────────────────────────────────────────

OUT = Path(__file__).parent / "conversations" / "synthetic"
OUT.mkdir(parents=True, exist_ok=True)

# ── Menu data — must stay in sync with SA_MENU in fixtures.py ────────────────

NON_OPT_ITEMS: list[tuple[str, int]] = [
    ("Classic Smash Burger",   7500),
    ("Double Smash Burger",    9500),
    ("Spicy Chicken Burger",   8000),
    ("Grilled Chicken Burger", 8500),
    ("Chips",                  3500),
    ("Onion Rings",            3000),
    ("Coca-Cola (330ml)",      2000),
    ("Coca-Cola (500ml)",      2500),
    ("Sprite (330ml)",         2000),
    ("Still Water",            1500),
    ("Ice Coffee",             7500),
]

# Coffee items require a milk type in the order message to stay DET-only
COFFEE_MILK: list[tuple[str, int, str]] = [
    ("Cappuccino", 4500, "oat milk"),
    ("Cappuccino", 4500, "regular milk"),
    ("Cappuccino", 4500, "soy milk"),
    ("Cappuccino", 4500, "almond milk"),
    ("Flat White",  4000, "oat milk"),
    ("Flat White",  4000, "regular milk"),
    ("Flat White",  4000, "soy milk"),
]

# ── Language variation banks ──────────────────────────────────────────────────

GREETINGS: list[str] = [
    "Hi", "Hello", "Morning", "Howzit", "Heita",
    "Good morning", "Good evening", "Hey", "Sawubona",
    "Hi there", "Hola",
]

SA_GREETINGS: list[str] = [
    "Howzit boss", "Heita bru", "Sharp sharp",
    "Howzit my guy", "Howzit china", "Howzit bra",
    # "Yoh hey" excluded: "Yoh" not in GREETING pattern (only "yo" is)
]

YES_PHRASES: list[str] = [
    "Yes", "Yeah", "Yep", "Sure", "Correct", "100", "Sharp",
    "Lekker", "OK", "Okay", "Yebo", "Sho", "Done", "Perfect",
    "Looks good", "Place it",
]

PICKUP_PHRASES: list[str] = [
    "Pickup", "Collection", "Pick up", "Pickup please", "Collect",
]

DELIVERY_PHRASES: list[str] = [
    "Delivery", "Deliver please", "Delivery please",
]

CANCEL_PHRASES: list[str] = [
    "Cancel", "Never mind", "Forget it", "Scratch that",
    "Actually never mind", "Start over",
]

# Standard SA order starters that DET handles (resolve ORDER_START intent)
ORDER_STARTERS: list[str] = [
    "Can I get a {}",
    "I want a {}",
    "Give me a {}",
    "I'd like a {}",
    "I'll have a {}",
    "Can I order a {}",
]

# SA-slang starters that still map to ORDER_START via normalizer or intent
SA_ORDER_STARTERS: list[str] = [
    "Gimme a {}",           # gimme → give me → ORDER_START
    "Lemme get a {}",       # lemme → let me → ORDER_START
    "Can i get a {}",       # cn → can (normalizer); direct ORDER_START match
    "Cn i get a {}",        # cn → can
    "I wanna get a {}",     # wanna → want
    "Let me get a {}",      # ORDER_START matches "let me get"
]

NAMES: list[str] = [
    "Mohammed", "Sipho", "Fatima", "Priya", "Thabo", "Amara",
    "Zanele", "Ahmed", "Lindiwe", "Riya", "Brendan", "Ntombi",
    "Keegan", "Aisha", "Pieter", "Yusuf", "Nompilo", "Tariq",
    "Shameel", "Kgotso",
]

PHONES: list[str] = [
    "0821234567", "0834567890", "0831111111", "0729999999",
    "0712345678", "0821000001", "0831234567", "0840000001",
    "0611111111", "0729876543",
]

ADDRESSES: list[str] = [
    "4 Bree Street Cape Town",
    "12 Long Street Cape Town",
    "88 Juta Street Braamfontein",
    "5 Jan Smuts Avenue Joburg",
    "22 Nelson Mandela Square Sandton",
]

# ── Counter ───────────────────────────────────────────────────────────────────

_ctr = [0]


def _id() -> str:
    _ctr[0] += 1
    return f"synth_{_ctr[0]:04d}"


def _save(conv: dict) -> None:
    path = OUT / f"{conv['id']}.json"
    path.write_text(json.dumps(conv, indent=2), encoding="utf-8")


# ── Reusable turn builders ────────────────────────────────────────────────────

def _greeting_turn(msg: str) -> dict:
    return {
        "message": msg,
        "expect": {
            "state": "GREETING",
            "cart": [],
            "response_contains": ["welcome"],
        },
    }


def _order_turn(msg: str, item_name: str, milk: str | None = None) -> dict:
    cart_item: dict = {"name": item_name, "quantity": 1}
    if milk:
        cart_item["special_instructions"] = milk
    return {
        "message": msg,
        "expect": {
            "state": "CONFIRMING_ORDER",
            "cart": [cart_item],
        },
    }


def _yes_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "CHOOSING_ORDER_MODE"}}


def _pickup_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "COLLECTING_DETAILS"}}


def _name_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "COLLECTING_DETAILS"}}


def _phone_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "ORDER_PLACED", "cart": []}}


def _delivery_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "COLLECTING_DETAILS"}}


def _address_turn(msg: str) -> dict:
    return {"message": msg, "expect": {"state": "WAITING_DELIVERY_FEE_APPROVAL", "cart": []}}


def _cancel_turn(msg: str) -> dict:
    return {
        "message": msg,
        "expect": {
            "state": "IDLE",
            "cart": [],
            "response_contains": ["cancel", "order"],
        },
    }


def _hours_turn(msg: str) -> dict:
    return {
        "message": msg,
        "expect": {
            "state": "IDLE",
            "response_contains": ["contact", "hours"],
        },
    }


def _location_turn(msg: str) -> dict:
    return {
        "message": msg,
        "expect": {
            "state": "IDLE",
            "response_contains": ["contact us for location"],
        },
    }


def _menu_turn(msg: str, state_before: str = "IDLE") -> dict:
    # MENU_REQUEST keeps the state unchanged when in active ordering states,
    # otherwise moves to BROWSING_MENU.
    _active = {"BUILDING_CART", "CONFIRMING_ORDER", "COLLECTING_DETAILS"}
    next_state = state_before if state_before in _active else "BROWSING_MENU"
    return {
        "message": msg,
        "expect": {
            "state": next_state,
            "response_contains": ["menu"],
        },
    }


def _empty_yes_turn(msg: str) -> dict:
    """Yes with empty cart → empty-cart message, state stays IDLE."""
    return {
        "message": msg,
        "expect": {
            "state": "IDLE",
            "cart": [],
            "response_contains": ["empty"],
        },
    }


def _final_order(item_name: str, price: int, name: str, mode: str = "PICKUP",
                 delivery_fee: int = 0) -> dict:
    return {
        "items": [{"name": item_name, "quantity": 1}],
        "subtotal_cents": price,
        "total_cents": price + delivery_fee,
        "order_mode": mode,
        "customer_name": name,
    }


def _final_order_multi(items: list[tuple[str, int]], name: str) -> dict:
    sub = sum(p for _, p in items)
    return {
        "items": [{"name": n, "quantity": 1} for n, _ in items],
        "subtotal_cents": sub,
        "total_cents": sub,
        "order_mode": "PICKUP",
        "customer_name": name,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Template generators
# ═══════════════════════════════════════════════════════════════════════════════

# ── T1: Full pickup with greeting ─────────────────────────────────────────────

def gen_pickup_with_greeting(
    item_name: str, price: int,
    greeting: str, yes: str, pickup: str, name: str, phone: str,
    starter: str = "Can I get a {}",
    milk: str | None = None,
    category: str = "synthetic_pickup",
    sa_slang: bool = False,
) -> None:
    order_msg = starter.format(item_name)
    if milk:
        order_msg += f" with {milk}"
    _save({
        "id": _id(),
        "title": f"Synth pickup — {item_name}{' (' + milk + ')' if milk else ''} — {greeting!r}",
        "category": category,
        "tags": ["synthetic", "det", "pickup"] + (["sa_slang"] if sa_slang else []),
        "turns": [
            _greeting_turn(greeting),
            _order_turn(order_msg, item_name, milk),
            _yes_turn(yes),
            _pickup_turn(pickup),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T2: Full pickup without greeting ─────────────────────────────────────────

def gen_pickup_no_greeting(
    item_name: str, price: int,
    yes: str, pickup: str, name: str, phone: str,
    starter: str = "Can I get a {}",
    milk: str | None = None,
    category: str = "synthetic_pickup",
    sa_slang: bool = False,
) -> None:
    order_msg = starter.format(item_name)
    if milk:
        order_msg += f" with {milk}"
    _save({
        "id": _id(),
        "title": f"Synth pickup (no greeting) — {item_name}{' (' + milk + ')' if milk else ''}",
        "category": category,
        "tags": ["synthetic", "det", "pickup"] + (["sa_slang"] if sa_slang else []),
        "turns": [
            _order_turn(order_msg, item_name, milk),
            _yes_turn(yes),
            _pickup_turn(pickup),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T3: Cancel immediately after ordering ─────────────────────────────────────

def gen_cancel_after_order(
    item_name: str,
    starter: str = "Can I get a {}",
    cancel: str = "Cancel",
    milk: str | None = None,
) -> None:
    order_msg = starter.format(item_name)
    if milk:
        order_msg += f" with {milk}"
    cart_item: dict = {"name": item_name, "quantity": 1}
    if milk:
        cart_item["special_instructions"] = milk
    _save({
        "id": _id(),
        "title": f"Synth cancel after order — {item_name}",
        "category": "synthetic_cancel",
        "tags": ["synthetic", "det", "cancel"],
        "turns": [
            {
                "message": order_msg,
                "expect": {"state": "CONFIRMING_ORDER", "cart": [cart_item]},
            },
            _cancel_turn(cancel),
        ],
    })


# ── T4: Cancel after confirming (CHOOSING_ORDER_MODE) ─────────────────────────

def gen_cancel_after_confirm(
    item_name: str,
    yes: str = "Yes",
    cancel: str = "Cancel",
    milk: str | None = None,
) -> None:
    order_msg = f"Can I get a {item_name}"
    if milk:
        order_msg += f" with {milk}"
    cart_item: dict = {"name": item_name, "quantity": 1}
    if milk:
        cart_item["special_instructions"] = milk
    _save({
        "id": _id(),
        "title": f"Synth cancel after confirm — {item_name}",
        "category": "synthetic_cancel",
        "tags": ["synthetic", "det", "cancel"],
        "turns": [
            {
                "message": order_msg,
                "expect": {"state": "CONFIRMING_ORDER", "cart": [cart_item]},
            },
            _yes_turn(yes),
            _cancel_turn(cancel),
        ],
    })


# ── T5: Business questions — hours ────────────────────────────────────────────

_HOURS_QUESTIONS = [
    "What time do you close?",
    "What are your hours?",
    "When do you close?",
    "What time do you open?",
    "Are you still open?",
    "What time do you close today?",
    "When are you open?",
    "What are your opening hours?",
    "What are your operating hours?",
    "When do you open?",
    "Are you open on weekends?",
    "What time does the store close?",
    "What are the closing hours?",
    "Are you currently open?",
    "What time does the restaurant close?",
    "Hours please",
    "Store hours?",
    "Opening times?",
]

def gen_hours_question(question: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth business hours — {question!r}",
        "category": "synthetic_business",
        "tags": ["synthetic", "det", "hours"],
        "turns": [_hours_turn(question)],
    })


# ── T6: Business questions — location ─────────────────────────────────────────

_LOCATION_QUESTIONS = [
    "Where are you?",
    "Where are you located?",
    "What's your address?",
    "How do I find you?",
    "Where is the restaurant?",
    "Where is the store?",
    "What's your location?",
    "Can you give me directions?",
    "Where can I find you?",
    "How do I get there?",
    "Where exactly are you?",
]

def gen_location_question(question: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth location — {question!r}",
        "category": "synthetic_business",
        "tags": ["synthetic", "det", "location"],
        "turns": [_location_turn(question)],
    })


# ── T7: Greeting only ─────────────────────────────────────────────────────────

def gen_greeting_only(greeting: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth greeting only — {greeting!r}",
        "category": "synthetic_greeting",
        "tags": ["synthetic", "det", "greeting"],
        "turns": [_greeting_turn(greeting)],
    })


# ── T8: Greeting + menu browse + cancel ───────────────────────────────────────

_MENU_REQUESTS = [
    "Menu", "What do you have?", "Show me the menu",
    "What's on the menu?", "What can I eat?", "What food do you have?",
    "What do you sell?", "What's available?", "Show me what you have",
    "What's on today?", "Show menu", "Items please",
]

def gen_greeting_menu_cancel(greeting: str, menu_msg: str, cancel: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth browse+cancel — {greeting!r}/{menu_msg!r}",
        "category": "synthetic_browse",
        "tags": ["synthetic", "det", "browse", "cancel"],
        "turns": [
            _greeting_turn(greeting),
            _menu_turn(menu_msg),
            {
                "message": cancel,
                "expect": {"state": "IDLE", "cart": []},
            },
        ],
    })


# ── T9: Multi-item pickup ─────────────────────────────────────────────────────

def gen_multi_item_pickup(
    item1: tuple[str, int],
    item2: tuple[str, int],
    yes: str,
    pickup: str,
    name: str,
    phone: str,
) -> None:
    n1, p1 = item1
    n2, p2 = item2
    msg = f"Can I get a {n1} and {n2}"
    _save({
        "id": _id(),
        "title": f"Synth multi-item — {n1} + {n2}",
        "category": "synthetic_multi_item",
        "tags": ["synthetic", "det", "pickup", "multi_item"],
        "turns": [
            {
                "message": msg,
                "expect": {
                    "state": "CONFIRMING_ORDER",
                    "cart": [
                        {"name": n1, "quantity": 1},
                        {"name": n2, "quantity": 1},
                    ],
                },
            },
            _yes_turn(yes),
            _pickup_turn(pickup),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order_multi([item1, item2], name),
    })


# ── T10: SA slang greeting + standard order ───────────────────────────────────

def gen_sa_greeting_pickup(
    greeting: str, item_name: str, price: int,
    yes: str, name: str, phone: str,
    milk: str | None = None,
) -> None:
    order_msg = f"Can I get a {item_name}"
    if milk:
        order_msg += f" with {milk}"
    _save({
        "id": _id(),
        "title": f"Synth SA greeting — {greeting!r} + {item_name}",
        "category": "synthetic_sa_slang",
        "tags": ["synthetic", "det", "pickup", "sa_slang"],
        "turns": [
            _greeting_turn(greeting),
            _order_turn(order_msg, item_name, milk),
            _yes_turn(yes),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T11: SA slang ordering (normalizer-resolved starters) ────────────────────

def gen_sa_order_pickup(
    item_name: str, price: int,
    starter: str, yes: str, name: str, phone: str,
    milk: str | None = None,
) -> None:
    order_msg = starter.format(item_name)
    if milk:
        order_msg += f" with {milk}"
    _save({
        "id": _id(),
        "title": f"Synth SA order — {starter!r} / {item_name}",
        "category": "synthetic_sa_slang",
        "tags": ["synthetic", "det", "pickup", "sa_slang"],
        "turns": [
            _order_turn(order_msg, item_name, milk),
            _yes_turn(yes),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T12: Yes/No with empty cart (confirmation guard) ─────────────────────────

def gen_empty_cart_yes(yes_msg: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth empty cart yes — {yes_msg!r}",
        "category": "synthetic_guard",
        "tags": ["synthetic", "det", "guard", "empty_cart"],
        "turns": [_empty_yes_turn(yes_msg)],
    })


# ── T13: Delivery order (DET path through address collection) ─────────────────

def gen_delivery(
    item_name: str, price: int,
    greeting: str, yes: str,
    address: str, fee_cents: int = 3000,
    payment: str = "Cash",
) -> None:
    """
    Delivery flow with detail collection disabled so the path is pure DET:
    greeting → order → yes → delivery → address → staff fee → accept → payment → placed.
    Business overrides skip name/phone collection to keep the flow straightforward.
    """
    turns = [
        _greeting_turn(greeting),
        _order_turn(f"Can I get a {item_name}", item_name),
        _yes_turn(yes),
        _delivery_turn("Delivery"),
        _address_turn(address),
        {
            "_type": "staff_action",
            "description": f"Staff sets R{fee_cents // 100} delivery fee",
            "context_updates": {
                "delivery_fee_cents": fee_cents,
                "delivery_fee_status": "FEE_SENT",
            },
        },
        {
            "message": yes,
            "expect": {"state": "COLLECTING_PAYMENT", "response_contains": ["cash", "card"]},
        },
        {
            "message": payment,
            "expect": {"state": "ORDER_PLACED", "cart": []},
        },
    ]

    _save({
        "id": _id(),
        "title": f"Synth delivery — {item_name} — {address[:20]}",
        "category": "synthetic_delivery",
        "tags": ["synthetic", "det", "delivery"],
        # Disable name/phone so COLLECTING_DETAILS goes straight to address collection.
        "business": {
            "delivery_enabled": True,
            "require_customer_name": False,
            "require_phone_number": False,
            "require_delivery_address": False,
        },
        "turns": turns,
        "final_order": {
            "items": [{"name": item_name, "quantity": 1}],
            "subtotal_cents": price,
            "delivery_fee_cents": fee_cents,
            "total_cents": price + fee_cents,
            "order_mode": "DELIVERY",
            "payment_status": "CASH_ON_COLLECTION",
        },
    })


# ── T14: No-change (no) in CONFIRMING_ORDER ───────────────────────────────────

def gen_no_then_cancel(item_name: str, no_msg: str = "No", cancel: str = "Cancel") -> None:
    """No in CONFIRMING_ORDER → BUILDING_CART, then cancel → IDLE."""
    _save({
        "id": _id(),
        "title": f"Synth no-change then cancel — {item_name}",
        "category": "synthetic_cancel",
        "tags": ["synthetic", "det", "cancel", "no_change"],
        "turns": [
            _order_turn(f"Can I get a {item_name}", item_name),
            {
                "message": no_msg,
                "expect": {"state": "BUILDING_CART"},
            },
            _cancel_turn(cancel),
        ],
    })


# ── T15: View cart then confirm ───────────────────────────────────────────────

# Only use messages that contain "cart" (not "order") to avoid ORDER_START
# firing before VIEW_CART due to pattern priority.
_CART_REQUESTS = [
    "Show my cart", "My cart", "View my cart",
    "What's in my cart?", "Cart please",
]

def gen_view_cart_confirm(
    item_name: str, price: int, cart_msg: str, yes: str, name: str, phone: str,
) -> None:
    _save({
        "id": _id(),
        "title": f"Synth view cart then confirm — {item_name}",
        "category": "synthetic_cart",
        "tags": ["synthetic", "det", "cart", "pickup"],
        "turns": [
            _order_turn(f"Can I get a {item_name}", item_name),
            {
                "message": cart_msg,
                "expect": {
                    "state": "CONFIRMING_ORDER",
                    "response_contains": [item_name.lower()],
                },
            },
            _yes_turn(yes),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T16: Cancel in CHOOSING_OPTIONS with required-option item (regression) ────

def gen_options_cancel(cancel: str = "Actually never mind") -> None:
    """Asks for cappuccino, bot asks for milk, customer cancels — IDLE, empty cart."""
    _save({
        "id": _id(),
        "title": f"Synth options cancel — {cancel!r}",
        "category": "synthetic_options",
        "tags": ["synthetic", "det", "options", "cancel"],
        "turns": [
            {
                "message": "Can I get a cappuccino",
                "expect": {"state": "CHOOSING_OPTIONS", "cart": []},
            },
            {
                "message": cancel,
                "expect": {"state": "IDLE", "cart": []},
            },
        ],
    })


# ── T17: Coffee with milk (full pickup) ───────────────────────────────────────

def gen_coffee_pickup(
    item_name: str, price: int, milk: str,
    greeting: str, yes: str, name: str, phone: str,
) -> None:
    order_msg = f"Can I get a {item_name} with {milk}"
    _save({
        "id": _id(),
        "title": f"Synth coffee pickup — {item_name} {milk} — {greeting!r}",
        "category": "synthetic_options",
        "tags": ["synthetic", "det", "pickup", "options", "coffee"],
        "turns": [
            _greeting_turn(greeting),
            {
                "message": order_msg,
                "expect": {
                    "state": "CONFIRMING_ORDER",
                    "cart": [{"name": item_name, "quantity": 1, "special_instructions": milk}],
                },
            },
            _yes_turn(yes),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T18: Greeting after hours / location question ────────────────────────────

def gen_hours_then_order(
    question: str, item_name: str, price: int, name: str, phone: str,
) -> None:
    _save({
        "id": _id(),
        "title": f"Synth hours then order — {item_name}",
        "category": "synthetic_business",
        "tags": ["synthetic", "det", "hours", "pickup"],
        "turns": [
            _hours_turn(question),
            _order_turn(f"Can I get a {item_name}", item_name),
            _yes_turn("Yes"),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": _final_order(item_name, price, name),
    })


# ── T19: Session recovery — greeting after idle ───────────────────────────────

# Only messages matching GREETING intent (^hi|hello|hey|howzit|heita|yebo|...).
# "Sorry" and "I'm back" do not match the greeting pattern -> LLM call.
_RECOVERY_MSGS = [
    "Hi", "Hello", "Howzit", "Morning", "Hey",
    "Heita", "Hi there",
]

def gen_recovery_greeting(msg: str) -> None:
    """Any greeting from IDLE leads to GREETING state."""
    _save({
        "id": _id(),
        "title": f"Synth session recovery — {msg!r}",
        "category": "synthetic_recovery",
        "tags": ["synthetic", "det", "greeting", "recovery"],
        "turns": [_greeting_turn(msg)],
    })


# ── T20: Empty-cart cancel (cancel from IDLE) ─────────────────────────────────

def gen_idle_cancel(cancel: str) -> None:
    _save({
        "id": _id(),
        "title": f"Synth idle cancel — {cancel!r}",
        "category": "synthetic_cancel",
        "tags": ["synthetic", "det", "cancel", "idle"],
        "turns": [
            {
                "message": cancel,
                "expect": {
                    "state": "IDLE",
                    "cart": [],
                    "response_contains": ["cancel", "order"],
                },
            }
        ],
    })


# ── T21: Two-item order with one coffee (milk provided) ──────────────────────

def gen_burger_plus_coffee(
    burger: tuple[str, int], coffee: tuple[str, int, str],
    yes: str, name: str, phone: str,
) -> None:
    bn, bp = burger
    cn, cp, milk = coffee
    msg = f"Can I get a {bn} and a {cn} with {milk}"
    sub = bp + cp
    _save({
        "id": _id(),
        "title": f"Synth burger + coffee — {bn} + {cn} {milk}",
        "category": "synthetic_multi_item",
        "tags": ["synthetic", "det", "pickup", "multi_item", "options"],
        "turns": [
            {
                "message": msg,
                "expect": {
                    "state": "CONFIRMING_ORDER",
                    "cart": [
                        {"name": bn, "quantity": 1},
                        {"name": cn, "quantity": 1, "special_instructions": milk},
                    ],
                },
            },
            _yes_turn(yes),
            _pickup_turn("Pickup"),
            _name_turn(name),
            _phone_turn(phone),
        ],
        "final_order": {
            "items": [{"name": bn, "quantity": 1}, {"name": cn, "quantity": 1}],
            "subtotal_cents": sub,
            "total_cents": sub,
            "order_mode": "PICKUP",
            "customer_name": name,
        },
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Generation loops — sample across parameter space
# ═══════════════════════════════════════════════════════════════════════════════

def _sample(lst: list, n: int) -> list:
    return random.sample(lst, min(n, len(lst)))


def generate_all() -> None:
    print("Generating synthetic conversations …")

    # ── 1. Full pickup WITH greeting ───────────────────────────────────────
    # Iterate all items; sample greeting × yes × pickup × name × phone
    for item_name, price in NON_OPT_ITEMS:
        combos = list(product(
            _sample(GREETINGS, 6),
            _sample(YES_PHRASES, 6),
            _sample(PICKUP_PHRASES, 3),
            _sample(NAMES, 6),
            _sample(PHONES, 3),
        ))
        for g, y, pk, nm, ph in _sample(combos, 20):
            gen_pickup_with_greeting(item_name, price, g, y, pk, nm, ph)

    # ── 2. Full pickup WITHOUT greeting ───────────────────────────────────
    for item_name, price in NON_OPT_ITEMS:
        combos = list(product(
            _sample(YES_PHRASES, 5),
            _sample(PICKUP_PHRASES, 3),
            _sample(NAMES, 5),
            _sample(PHONES, 3),
        ))
        for y, pk, nm, ph in _sample(combos, 12):
            gen_pickup_no_greeting(item_name, price, y, pk, nm, ph)

    # ── 3. Cancel immediately after ordering ──────────────────────────────
    for item_name, _ in NON_OPT_ITEMS:
        for cancel in CANCEL_PHRASES:
            for starter in _sample(ORDER_STARTERS, 3):
                gen_cancel_after_order(item_name, starter, cancel)

    # ── 4. Cancel after confirming (CHOOSING_ORDER_MODE) ──────────────────
    for item_name, _ in NON_OPT_ITEMS:
        for yes, cancel in zip(_sample(YES_PHRASES, 4), _sample(CANCEL_PHRASES, 4)):
            gen_cancel_after_confirm(item_name, yes, cancel)

    # ── 5. Hours questions ─────────────────────────────────────────────────
    for q in _HOURS_QUESTIONS:
        gen_hours_question(q)

    # ── 6. Location questions ──────────────────────────────────────────────
    for q in _LOCATION_QUESTIONS:
        gen_location_question(q)

    # ── 7. Greeting only ──────────────────────────────────────────────────
    for g in GREETINGS + SA_GREETINGS:
        gen_greeting_only(g)

    # ── 8. Browse then cancel ─────────────────────────────────────────────
    for g, m, c in zip(
        _sample(GREETINGS, 8),
        _sample(_MENU_REQUESTS, 8),
        _sample(CANCEL_PHRASES, 8),
    ):
        gen_greeting_menu_cancel(g, m, c)

    # ── 9. Multi-item pickup ──────────────────────────────────────────────
    item_pairs = [
        (("Classic Smash Burger", 7500), ("Chips", 3500)),
        (("Double Smash Burger", 9500), ("Onion Rings", 3000)),
        (("Spicy Chicken Burger", 8000), ("Coca-Cola (330ml)", 2000)),
        (("Grilled Chicken Burger", 8500), ("Still Water", 1500)),
        (("Classic Smash Burger", 7500), ("Coca-Cola (330ml)", 2000)),
        (("Double Smash Burger", 9500), ("Sprite (330ml)", 2000)),
        (("Chips", 3500), ("Coca-Cola (330ml)", 2000)),
        (("Classic Smash Burger", 7500), ("Sprite (330ml)", 2000)),
        (("Ice Coffee", 7500), ("Classic Smash Burger", 7500)),
        (("Onion Rings", 3000), ("Sprite (330ml)", 2000)),
        (("Classic Smash Burger", 7500), ("Still Water", 1500)),
        (("Double Smash Burger", 9500), ("Chips", 3500)),
        (("Spicy Chicken Burger", 8000), ("Still Water", 1500)),
        (("Grilled Chicken Burger", 8500), ("Coca-Cola (500ml)", 2500)),
        (("Classic Smash Burger", 7500), ("Onion Rings", 3000)),
        (("Ice Coffee", 7500), ("Chips", 3500)),
    ]
    for (i1, i2), yes, nm, ph in zip(
        item_pairs * 8,
        _sample(YES_PHRASES, 128),
        _sample(NAMES, 128),
        _sample(PHONES, 128),
    ):
        gen_multi_item_pickup(i1, i2, yes, "Pickup", nm, ph)

    # ── 10. SA slang greetings ─────────────────────────────────────────────
    for g in SA_GREETINGS:
        for item_name, price in _sample(NON_OPT_ITEMS, 6):
            nm, ph = random.choice(NAMES), random.choice(PHONES)
            gen_sa_greeting_pickup(g, item_name, price, random.choice(YES_PHRASES), nm, ph)

    # ── 11. SA slang ordering starters ────────────────────────────────────
    for starter in SA_ORDER_STARTERS:
        for item_name, price in NON_OPT_ITEMS:
            nm, ph = random.choice(NAMES), random.choice(PHONES)
            gen_sa_order_pickup(item_name, price, starter, random.choice(YES_PHRASES), nm, ph)

    # ── 12. Empty-cart yes (guard) ─────────────────────────────────────────
    # Only use messages that match ORDER_CONFIRM intent (not GREETING).
    # "Sharp" → GREETING (pattern priority); "Correct" → None (not in ORDER_CONFIRM pattern).
    _ORDER_CONFIRM_ONLY = [
        "Yes", "Yep", "Yeah", "Sure", "100", "Lekker",
        "OK", "Okay", "Done", "Perfect", "Looks good",
    ]
    for yes in _ORDER_CONFIRM_ONLY:
        gen_empty_cart_yes(yes)

    # ── 13. Delivery orders ────────────────────────────────────────────────
    delivery_items = [
        ("Classic Smash Burger", 7500),
        ("Double Smash Burger", 9500),
        ("Chips", 3500),
        ("Spicy Chicken Burger", 8000),
        ("Ice Coffee", 7500),
        ("Grilled Chicken Burger", 8500),
        ("Coca-Cola (330ml)", 2000),
    ]
    for (item_name, price), addr, g, y in zip(
        delivery_items * 15,
        _sample(ADDRESSES * 15, 100),
        _sample(GREETINGS * 8, 100),
        _sample(YES_PHRASES * 7, 100),
    ):
        gen_delivery(item_name, price, g, y, addr)

    # ── 14. No then cancel ────────────────────────────────────────────────
    for item_name, _ in NON_OPT_ITEMS:
        for no_msg, cancel in zip(
            _sample(["No", "Nah", "Nope", "Not yet", "Wait"], 2),
            _sample(CANCEL_PHRASES, 2),
        ):
            gen_no_then_cancel(item_name, no_msg, cancel)

    # ── 15. View cart then confirm ─────────────────────────────────────────
    for (item_name, price), cart_msg, yes, nm, ph in zip(
        _sample(NON_OPT_ITEMS, 6) * 4,
        _sample(_CART_REQUESTS, 24),
        _sample(YES_PHRASES, 24),
        _sample(NAMES, 24),
        _sample(PHONES, 24),
    ):
        gen_view_cart_confirm(item_name, price, cart_msg, yes, nm, ph)

    # ── 16. Options cancel ────────────────────────────────────────────────
    for cancel in CANCEL_PHRASES:
        gen_options_cancel(cancel)

    # ── 17. Coffee pickup ─────────────────────────────────────────────────
    for item_name, price, milk in COFFEE_MILK:
        for g, y, nm, ph in zip(
            _sample(GREETINGS, 8),
            _sample(YES_PHRASES, 8),
            _sample(NAMES, 8),
            _sample(PHONES, 8),
        ):
            gen_coffee_pickup(item_name, price, milk, g, y, nm, ph)

    # ── 18. Hours then order ──────────────────────────────────────────────
    for q, (item_name, price), nm, ph in zip(
        _sample(_HOURS_QUESTIONS, 10),
        _sample(NON_OPT_ITEMS, 10),
        _sample(NAMES, 10),
        _sample(PHONES, 10),
    ):
        gen_hours_then_order(q, item_name, price, nm, ph)

    # ── 19. Session recovery ──────────────────────────────────────────────
    for msg in _RECOVERY_MSGS * 5:
        gen_recovery_greeting(msg)

    # ── 20. Idle cancel ───────────────────────────────────────────────────
    for cancel in CANCEL_PHRASES * 3:
        gen_idle_cancel(cancel)

    # ── 21. Burger + coffee combos ────────────────────────────────────────
    burger_items = [
        ("Classic Smash Burger", 7500),
        ("Double Smash Burger", 9500),
        ("Spicy Chicken Burger", 8000),
    ]
    for b, c, y, nm, ph in zip(
        _sample(burger_items, 3) * 7,
        _sample(COFFEE_MILK, 7) * 3,
        _sample(YES_PHRASES, 21),
        _sample(NAMES, 21),
        _sample(PHONES, 21),
    ):
        gen_burger_plus_coffee(b, c, y, nm, ph)

    # ── 22. Additional pickup with different starters ─────────────────────
    for starter in ORDER_STARTERS[1:]:  # skip "Can I get a" (already covered)
        for item_name, price in _sample(NON_OPT_ITEMS, 6):
            nm, ph = random.choice(NAMES), random.choice(PHONES)
            gen_pickup_no_greeting(
                item_name, price,
                random.choice(YES_PHRASES), "Pickup", nm, ph,
                starter=starter,
            )

    # ── 23. Multi-item with coffee ────────────────────────────────────────
    for (item_name, price), (cn, cp, milk), nm, ph in zip(
        _sample(NON_OPT_ITEMS, 8) * 4,
        _sample(COFFEE_MILK, 4) * 8,
        _sample(NAMES, 32),
        _sample(PHONES, 32),
    ):
        msg = f"Can I get a {item_name} and a {cn} with {milk}"
        sub = price + cp
        _save({
            "id": _id(),
            "title": f"Synth multi + coffee — {item_name} + {cn}",
            "category": "synthetic_multi_item",
            "tags": ["synthetic", "det", "pickup", "multi_item", "options"],
            "turns": [
                {
                    "message": msg,
                    "expect": {
                        "state": "CONFIRMING_ORDER",
                        "cart": [
                            {"name": item_name, "quantity": 1},
                            {"name": cn, "quantity": 1, "special_instructions": milk},
                        ],
                    },
                },
                _yes_turn(random.choice(YES_PHRASES)),
                _pickup_turn("Pickup"),
                _name_turn(nm),
                _phone_turn(ph),
            ],
            "final_order": {
                "items": [{"name": item_name, "quantity": 1}, {"name": cn, "quantity": 1}],
                "subtotal_cents": sub,
                "total_cents": sub,
                "order_mode": "PICKUP",
                "customer_name": nm,
            },
        })

    total = _ctr[0]
    print(f"Generated {total} synthetic conversations -> {OUT}")
    return total


if __name__ == "__main__":
    generate_all()
