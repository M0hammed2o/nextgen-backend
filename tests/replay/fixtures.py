"""
Shared fixtures for the conversation replay suite.

Provides:
  - FakeBusiness   — duck-typed Business model substitute
  - FakeCustomer   — duck-typed Customer substitute
  - FakeOrder      — captures what create_order_from_cart would write to DB
  - FakeMenuItem   — duck-typed MenuItem
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


class FakeMenuItem:
    """Duck-typed MenuItem."""

    def __init__(self, name: str, price_cents: int, category_name: str = "Burgers"):
        self.id = uuid.uuid4()
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False
        self.options_json = {}
        self.category_id = None
        self.sort_order = 0
        self.description = None


# ── Standard South African fast-food menu ─────────────────────────────────────
# Prices match the expected totals in the conversation test matrix.

SA_MENU: list[FakeMenuItem] = [
    # Burgers
    FakeMenuItem("Classic Smash Burger",   7500),
    FakeMenuItem("Double Smash Burger",    9500),
    FakeMenuItem("Spicy Chicken Burger",   8000),
    FakeMenuItem("Grilled Chicken Burger", 8500),
    # Sides
    FakeMenuItem("Chips",                  3500),
    FakeMenuItem("Onion Rings",            3000),
    # Drinks
    FakeMenuItem("Coca-Cola (330ml)",      2000),
    FakeMenuItem("Coca-Cola (500ml)",      2500),
    FakeMenuItem("Sprite (330ml)",         2000),
    FakeMenuItem("Still Water",            1500),
    FakeMenuItem("Ice Coffee",             7500),
]

# Quick lookup: name → FakeMenuItem
SA_MENU_MAP: dict[str, FakeMenuItem] = {i.name.lower(): i for i in SA_MENU}


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
