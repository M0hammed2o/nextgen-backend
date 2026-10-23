"""
Shared fixtures for the conversation replay suite.

Provides:
  - FakeBusiness   — duck-typed Business model substitute
  - FakeCustomer   — duck-typed Customer substitute
  - FakeOrder      — captures what create_order_from_cart would write to DB
  - FakeMenuItem   — duck-typed MenuItem (Phase 8: supports add_ons)
  - SA_MENU        — standard South African fast-food menu used across replays
  - make_business  — factory that merges per-conversation overrides
"""

import uuid


class FakeBusiness:
    """Duck-typed Business. Matches every attribute the pipeline reads."""

    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.name = kw.get("name", "NextGen Smash Burgers")
        self.whatsapp_phone_number_id = "test_phone_id"
        self.currency = kw.get("currency", "ZAR")
        self.delivery_enabled = kw.get("delivery_enabled", True)
        self.order_in_only = kw.get("order_in_only", False)
        self.require_customer_name = kw.get("require_customer_name", True)
        self.require_phone_number = kw.get("require_phone_number", True)
        self.require_delivery_address = kw.get("require_delivery_address", False)
        self.greeting_text = kw.get("greeting_text", "Welcome to NextGen Smash Burgers! 🍔")
        self.fallback_text = kw.get("fallback_text", "Sorry, I didn't understand that.")
        self.closed_text = kw.get("closed_text", None)
        self.business_hours = kw.get("business_hours", None)   # None = always open
        self.timezone = kw.get("timezone", "Africa/Johannesburg")
        self.menu_image_url = kw.get("menu_image_url", None)
        self.order_number_sequence = 0
        self.is_active = True
        self.is_whatsapp_enabled = True
        self.location_text = kw.get("location_text", None)
        self.location_url = kw.get("location_url", None)
        # prompt_builder attributes
        self.address = kw.get("address", None)
        self.phone = kw.get("phone", None)


class FakeCustomer:
    """Duck-typed Customer."""

    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.business_id = kw.get("business_id", uuid.uuid4())
        self.wa_id = kw.get("wa_id", "27837866021")
        self.display_name = kw.get("display_name", None)
        self.phone_number = kw.get("phone_number", None)
        self.opted_out = False
        self.last_message_at = None


class FakeOrder:
    """
    Captures the order that would be written to the database.
    Created by the mock replacement for order_creator.create_order_from_cart.
    """

    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.order_number = kw.get("order_number", "BO-001")
        self.status = kw.get("status", "NEW")
        self.order_mode = kw.get("order_mode", "PICKUP")
        self.subtotal_cents = kw.get("subtotal_cents", 0)
        self.delivery_fee_cents = kw.get("delivery_fee_cents", 0)
        self.total_cents = kw.get("total_cents", 0)
        self.currency = kw.get("currency", "ZAR")
        self.customer_name = kw.get("customer_name", None)
        self.phone_number = kw.get("phone_number", None)
        self.delivery_address = kw.get("delivery_address", None)
        self.items = kw.get("items", [])   # list of cart-item dicts
        self.payment_status = kw.get("payment_status", None)
        self.special_instructions = kw.get("special_instructions", None)


class FakeMenuItem:
    """
    Duck-typed MenuItem.

    Phase 8: add_ons is a list of dicts matching the structure returned by
    _load_add_ons_map so the replay framework can mock that function directly
    from the menu items without a real DB.

    add_ons format: [{"add_on_id": str, "name": str, "price_cents": int,
                       "min_qty": int, "max_qty": int, "default_qty": int}]
    """

    def __init__(
        self,
        name: str,
        price_cents: int,
        category_name: str = "Burgers",
        options_json: dict | None = None,
        add_ons: list[dict] | None = None,
        description: str | None = None,
        image_url: str | None = None,
    ):
        self.id = uuid.uuid4()
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False
        self.options_json = options_json or {}
        self.category_id = None
        self.sort_order = 0
        self.description = description
        # image_url: MenuItem.image_url is a stored column, resolved once at
        # write time from image_asset_id (routes_menu.py::_resolve_item_image)
        # -- never re-derived live, so the fake just needs the plain string.
        self.image_url = image_url
        # Phase 8: paid add-ons available on this item
        self.add_ons = add_ons or []


# ── Option group definitions ──────────────────────────────────────────────────

# Coffee milk options without price delta — used by existing conversations.
# Existing replay tests select "oat milk" via special_instructions, not the
# options dict, so subtotals in those tests remain unchanged.
_COFFEE_MILK_OPTIONS = {
    "option_groups": [
        {
            "id": "milk_type",
            "name": "Milk Type",
            "required": True,
            "min_selections": 1,
            "max_selections": 1,
            "sort_order": 0,
            "is_enabled": True,
            "options": [
                {"id": "regular", "name": "Regular milk", "price_delta_cents": 0,    "sort_order": 0, "is_enabled": True},
                {"id": "soy",     "name": "Soy milk",     "price_delta_cents": 0,    "sort_order": 1, "is_enabled": True},
                {"id": "oat",     "name": "Oat milk",     "price_delta_cents": 0,    "sort_order": 2, "is_enabled": True},
                {"id": "almond",  "name": "Almond milk",  "price_delta_cents": 0,    "sort_order": 3, "is_enabled": True},
            ],
        },
    ]
}

_FLAT_WHITE_OPTIONS = {
    "option_groups": [
        {
            "id": "milk_type",
            "name": "Milk Type",
            "required": True,
            "min_selections": 1,
            "max_selections": 1,
            "sort_order": 0,
            "is_enabled": True,
            "options": [
                {"id": "regular", "name": "Regular milk", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "soy",     "name": "Soy milk",     "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
                {"id": "oat",     "name": "Oat milk",     "price_delta_cents": 0, "sort_order": 2, "is_enabled": True},
            ],
        },
        {
            "id": "extras",
            "name": "Extras",
            "required": False,
            "min_selections": 0,
            "max_selections": 2,
            "sort_order": 1,
            "is_enabled": True,
            "options": [
                {"id": "extra_shot", "name": "Extra shot", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "decaf",      "name": "Decaf",      "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
            ],
        },
    ]
}

# Combo included-drink choice — all options are price_delta_cents=0 because
# the drink is already bundled into the combo's own price. Used by the
# combo-pricing regression conversations (conv_321/322/323) added to pin the
# BO-000004 production bug: a combo's included components must never be
# charged twice, and letting the customer pick WHICH included drink they get
# must never add a separate beverage charge.
_COMBO_DRINK_CHOICE_OPTIONS = {
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
                {"id": "coke",   "name": "Coca-Cola", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "sprite", "name": "Sprite",     "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
                {"id": "water",  "name": "Still Water","price_delta_cents": 0, "sort_order": 2, "is_enabled": True},
            ],
        },
    ]
}

# Real Mochachos Spice Level structure (Lemon & Herb/Mild/Hot/Extra Hot/
# Dynamite, all price_delta_cents=0) -- used by the realistic Mochachos
# end-to-end conversations below (conv_334+).
_MOCHACHOS_SPICE_LEVEL_OPTIONS = {
    "option_groups": [
        {
            "id": "spice-level",
            "name": "Spice Level",
            "required": True,
            "min_selections": 1,
            "max_selections": 1,
            "sort_order": 0,
            "is_enabled": True,
            "options": [
                {"id": "lemon-herb", "name": "Lemon and Herb", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "mild", "name": "Mild", "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
                {"id": "hot", "name": "Hot", "price_delta_cents": 0, "sort_order": 2, "is_enabled": True},
                {"id": "extra-hot", "name": "Extra Hot", "price_delta_cents": 0, "sort_order": 3, "is_enabled": True},
                {"id": "dynamite", "name": "Dynamite", "price_delta_cents": 0, "sort_order": 4, "is_enabled": True},
            ],
        },
    ]
}

# Phase 8: Priced milk options — used by new pricing replay conversations.
# Full Cream = no charge, Soy/Oat = +R10, Almond = +R15.
_PRICED_MILK_OPTIONS = {
    "option_groups": [
        {
            "id": "milk",
            "name": "Milk",
            "required": True,
            "min_selections": 1,
            "max_selections": 1,
            "sort_order": 0,
            "is_enabled": True,
            "options": [
                {"id": "fc",     "name": "Full Cream",  "price_delta_cents": 0,    "sort_order": 0, "is_enabled": True},
                {"id": "soy",    "name": "Soy Milk",    "price_delta_cents": 1000, "sort_order": 1, "is_enabled": True},
                {"id": "oat",    "name": "Oat Milk",    "price_delta_cents": 1000, "sort_order": 2, "is_enabled": True},
                {"id": "almond", "name": "Almond Milk", "price_delta_cents": 1500, "sort_order": 3, "is_enabled": True},
            ],
        },
    ]
}

# ── Add-on definitions (shared across items) ──────────────────────────────────

_BURGER_ADD_ONS = [
    {"add_on_id": "extra-cheese", "name": "Extra Cheese", "price_cents": 1000, "min_qty": 0, "max_qty": 3, "default_qty": 1},
    {"add_on_id": "extra-patty",  "name": "Extra Patty",  "price_cents": 2500, "min_qty": 0, "max_qty": 3, "default_qty": 1},
    {"add_on_id": "extra-bacon",  "name": "Extra Bacon",  "price_cents": 1500, "min_qty": 0, "max_qty": 2, "default_qty": 1},
]

_COFFEE_ADD_ONS = [
    {"add_on_id": "extra-shot",  "name": "Extra Shot",  "price_cents": 1500, "min_qty": 0, "max_qty": 2, "default_qty": 1},
    {"add_on_id": "whipped-cream","name": "Whipped Cream","price_cents": 500, "min_qty": 0, "max_qty": 1, "default_qty": 1},
]


# ── Standard South African fast-food menu ─────────────────────────────────────
# Prices match the expected totals in the conversation test matrix.
#
# Phase 8 additions:
# - Burgers now have add_ons (Extra Cheese, Extra Patty, Extra Bacon)
# - Latte: new item with PRICED milk options (+R10/R15) and coffee add-ons
# - Existing Cappuccino/Flat White keep price_delta_cents=0 so existing
#   conversation subtotals are unchanged.

SA_MENU: list[FakeMenuItem] = [
    # Burgers — all carry paid add-ons matching production DB configuration.
    # Classic Smash Burger now has _BURGER_ADD_ONS so replay tests match the
    # live menu where Extra Cheese (+R10) and Extra Patty (+R25) are linked.
    FakeMenuItem("Classic Smash Burger",   7500, add_ons=_BURGER_ADD_ONS),
    FakeMenuItem("Double Smash Burger",    9500, add_ons=_BURGER_ADD_ONS),
    FakeMenuItem("Spicy Chicken Burger",   8000, add_ons=_BURGER_ADD_ONS),
    FakeMenuItem("Grilled Chicken Burger", 8500, add_ons=_BURGER_ADD_ONS),
    FakeMenuItem("Loaded Smash Burger",    7500, add_ons=_BURGER_ADD_ONS),
    # Sides
    FakeMenuItem("Chips",                  3500),
    FakeMenuItem("Onion Rings",            3000),
    # Drinks
    FakeMenuItem("Coca-Cola (330ml)",      2000),
    FakeMenuItem("Coca-Cola (500ml)",      2500),
    FakeMenuItem("Sprite (330ml)",         2000),
    FakeMenuItem("Still Water",            1500),
    FakeMenuItem("Ice Coffee",             7500),
    # Coffee (existing — price_delta_cents=0, backward compat with existing tests)
    FakeMenuItem("Cappuccino",  4500, category_name="Coffee", options_json=_COFFEE_MILK_OPTIONS),
    FakeMenuItem("Flat White",  4000, category_name="Coffee", options_json=_FLAT_WHITE_OPTIONS),
    # Phase 8: Latte with PRICED milk options and coffee add-ons
    FakeMenuItem("Latte",  4500, category_name="Coffee",
                 options_json=_PRICED_MILK_OPTIONS, add_ons=_COFFEE_ADD_ONS),
    # Pizzas — added for the specials/menu-intent-override regression suite
    # (tests/test_bot_specials_override_regression.py). Added-only: no existing
    # replay conversation references these names, so existing totals/assertions
    # are unaffected.
    FakeMenuItem("Margherita Pizza", 9000, category_name="Pizza"),
    FakeMenuItem("Pepperoni Pizza",  10500, category_name="Pizza"),
    # Combo — added-only, for the combo-pricing regression suite
    # (conv_321/322/323). A combo is modelled exactly like production's real
    # Mochachos combos: ONE flat-priced item whose included components
    # (burger, chips, drink) exist only in descriptive text, never as
    # separate cart lines. See _COMBO_DRINK_CHOICE_OPTIONS above.
    FakeMenuItem("Smash Burger Combo", 12000, options_json=_COMBO_DRINK_CHOICE_OPTIONS),
    # Secret-menu-style items with a real image_url — added-only, for the
    # WhatsApp image-request replay suite (conv_328+). "The Jawbreaker"
    # matches the real Mochachos Secret Creations product name used
    # throughout this feature's design discussion. "The Nightcap" has no
    # image on purpose, to prove the same request path degrades to an
    # accurate text-only reply rather than a broken/missing image.
    FakeMenuItem(
        "The Jawbreaker", 26000, category_name="Secret Creations",
        description="Two beef patties, double cheese, jalapeños and our secret sauce.",
        image_url="https://example.test/assets/the-jawbreaker.jpg",
    ),
    FakeMenuItem(
        "The Nightcap", 14000, category_name="Secret Creations",
        description="A late-night favourite — ask your server for details.",
    ),
    # Real Mochachos item names/prices (as currently live, not the pending
    # Westville correction) — added-only, for the realistic end-to-end
    # Mochachos customer-conversation suite (conv_334+). Not used by any
    # pre-existing fixture, so nothing above is affected.
    FakeMenuItem("Quarter Chicken", 5990, category_name="Flame-Grilled Chicken",
                 options_json=_MOCHACHOS_SPICE_LEVEL_OPTIONS),
    FakeMenuItem("Buddy 440ml", 2890, category_name="Drinks"),
    FakeMenuItem(
        "Quarter Chicken, Beeg Chips and a Buddy", 10990, category_name="Combos",
        description="Quarter chicken, chips, 440ml soft drink",
        options_json=_MOCHACHOS_SPICE_LEVEL_OPTIONS,
    ),
]

# Quick lookup: name → FakeMenuItem
SA_MENU_MAP: dict[str, FakeMenuItem] = {i.name.lower(): i for i in SA_MENU}


class FakeSpecial:
    """Duck-typed Special. Matches every attribute responses.py/prompt_builder.py read."""

    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.title = kw.get("title", "Special")
        self.description = kw.get("description", None)
        self.is_active = kw.get("is_active", True)
        self.sort_order = kw.get("sort_order", 0)
        self.image_url = kw.get("image_url", None)
        self.days_of_week = kw.get("days_of_week", None)
        self.start_at = kw.get("start_at", None)
        self.end_at = kw.get("end_at", None)


def make_business(overrides: dict | None = None) -> FakeBusiness:
    """Create a FakeBusiness, applying per-conversation overrides."""
    return FakeBusiness(**(overrides or {}))


def make_customer(**kw) -> FakeCustomer:
    return FakeCustomer(**kw)


def menu_from_names(names: list[str]) -> list[FakeMenuItem]:
    """Return a subset of SA_MENU by item names (for smaller menus in tests)."""
    result = []
    for n in names:
        item = SA_MENU_MAP.get(n.lower())
        if item:
            result.append(item)
    return result
