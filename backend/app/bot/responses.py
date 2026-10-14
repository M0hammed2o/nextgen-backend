"""
Template Responses — pre-built responses that don't need LLM.
These handle ~60-70% of messages at zero cost.

All responses use WhatsApp markdown:
  *bold*  _italic_  ~strikethrough~  ```monospace```
"""

from datetime import date

from shared.models.business import Business
from shared.models.menu import MenuCategory, MenuItem
from shared.models.specials import Special
from shared.utils.money import format_currency
from shared.utils.time import is_business_open, to_business_tz, utc_now


def greeting_response(business: Business) -> str:
    """Return the business greeting text or a default."""
    if business.greeting_text:
        return business.greeting_text
    return (
        f"Welcome to *{business.name}*! 👋\n\n"
        "How can I help you today?\n\n"
        "You can say:\n"
        '• "menu" to see our menu\n'
        '• "specials" for today\'s deals\n'
        '• "hours" for opening times\n'
        '• "order" to start an order'
    )


def menu_response(
    categories: list[MenuCategory],
    items: list[MenuItem],
    currency: str = "ZAR",
) -> str:
    """
    Build a full menu response, grouped by category.
    Only includes active, non-deleted items.
    """
    if not items:
        return "Our menu is currently being updated. Please check back soon!"

    # Group items by category
    cat_map: dict[str | None, list[MenuItem]] = {}
    cat_names: dict[str | None, str] = {None: "Other"}
    for cat in categories:
        cat_names[str(cat.id)] = cat.name

    for item in items:
        if not item.is_active or item.is_deleted:
            continue
        key = str(item.category_id) if item.category_id else None
        cat_map.setdefault(key, []).append(item)

    lines = ["📋 *Our Menu*\n"]

    # Order by category sort_order
    sorted_cats = sorted(categories, key=lambda c: c.sort_order)
    for cat in sorted_cats:
        cat_key = str(cat.id)
        cat_items = cat_map.get(cat_key, [])
        if not cat_items:
            continue

        lines.append(f"\n*{cat.name}*")
        for item in sorted(cat_items, key=lambda i: i.sort_order):
            price = format_currency(item.price_cents, currency)
            lines.append(f"  • {item.name} — {price}")
            if item.description:
                lines.append(f"    _{item.description}_")

    # Uncategorized items
    uncategorized = cat_map.get(None, [])
    if uncategorized:
        lines.append("\n*Other*")
        for item in uncategorized:
            price = format_currency(item.price_cents, currency)
            lines.append(f"  • {item.name} — {price}")

    lines.append('\nTo order, just tell me what you\'d like! 🍽️')
    return "\n".join(lines)


# Meta's WhatsApp Cloud API hard limit on a single text message body is
# 4096 characters. This stays comfortably under that so formatting
# (markdown, emoji) never pushes a message over the real limit.
WHATSAPP_TEXT_SAFE_MAX_CHARS = 4000


def build_menu_messages(
    categories: list[MenuCategory],
    items: list[MenuItem],
    currency: str = "ZAR",
    max_chars: int = WHATSAPP_TEXT_SAFE_MAX_CHARS,
) -> list[str]:
    """
    Same content and formatting as menu_response(), but split into one or
    more WhatsApp-safe text messages when the full menu would exceed
    max_chars (BUG-0147). Deliberately independent of menu_response()'s own
    line-joining — this function never touches menu_response() or changes
    its behaviour, so every existing caller/test of menu_response() is
    unaffected.

    Splits only at category or item boundaries: a price or description line
    is never separated from the item it belongs to. If a single category's
    items don't fit in one message on their own, it is further split
    between items (still keeping each item with its own description),
    re-emitting a "(cont.)" category header in the follow-on message so
    context isn't lost. Returns exactly one message (the whole menu) in the
    common case where it already fits under max_chars.
    """
    if not items:
        return ["Our menu is currently being updated. Please check back soon!"]

    cat_map: dict[str | None, list[MenuItem]] = {}
    for cat in categories:
        cat_map[str(cat.id)] = []
    for item in items:
        if not item.is_active or item.is_deleted:
            continue
        key = str(item.category_id) if item.category_id else None
        cat_map.setdefault(key, []).append(item)

    def _item_unit(item: MenuItem) -> list[str]:
        price = format_currency(item.price_cents, currency)
        unit = [f"  • {item.name} — {price}"]
        if item.description:
            unit.append(f"    _{item.description}_")
        return unit

    sections: list[tuple[str, list[list[str]]]] = []
    sorted_cats = sorted(categories, key=lambda c: c.sort_order)
    for cat in sorted_cats:
        cat_items = cat_map.get(str(cat.id), [])
        if not cat_items:
            continue
        units = [_item_unit(item) for item in sorted(cat_items, key=lambda i: i.sort_order)]
        sections.append((f"*{cat.name}*", units))

    uncategorized = cat_map.get(None, [])
    if uncategorized:
        sections.append(("*Other*", [_item_unit(item) for item in uncategorized]))

    header = "📋 *Our Menu*"
    footer = "To order, just tell me what you'd like! 🍽️"

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            messages.append("\n".join(current))
        current = []
        current_len = 0

    for section_header, units in sections:
        header_len = len(section_header) + 1
        if current and current_len + header_len > max_chars:
            _flush()
        current.append(section_header)
        current_len += header_len

        for unit in units:
            unit_text = "\n".join(unit)
            unit_len = len(unit_text) + 1
            if current and current_len + unit_len > max_chars:
                _flush()
                cont_header = f"{section_header} (cont.)"
                current = [cont_header]
                current_len = len(cont_header) + 1
            current.extend(unit)
            current_len += unit_len

    footer_len = len(footer) + 1
    if current and current_len + footer_len > max_chars:
        _flush()
    current.append(footer)
    _flush()

    return messages


def get_todays_active_specials(specials: list[Special]) -> list[Special]:
    """Filter a list of specials to those active today."""
    today_name = date.today().strftime("%a").lower()[:3]
    active_today = []
    now = utc_now()
    for s in specials:
        if not s.is_active:
            continue
        # Check day-of-week filter (supports both "mon" and "Monday" formats)
        if s.days_of_week:
            days_lower = [d.lower()[:3] for d in s.days_of_week]
            if today_name not in days_lower:
                continue
        # Check date range
        if s.start_at and now < s.start_at:
            continue
        if s.end_at and now > s.end_at:
            continue
        active_today.append(s)
    return active_today


def specials_response(specials: list[Special], currency: str = "ZAR") -> str:
    """Build specials response, filtering to today's active specials."""
    if not specials:
        return "We don't have any specials running right now. Check back soon! 🤞"

    active_today = get_todays_active_specials(specials)

    if not active_today:
        return "No specials today, but check back tomorrow! 🤞"

    lines = ["🔥 *Today's Specials*\n"]
    for s in sorted(active_today, key=lambda x: x.sort_order):
        lines.append(f"⭐ *{s.title}*")
        if s.description:
            lines.append(f"   {s.description}")
    lines.append("\nAsk me about any of these!")
    return "\n".join(lines)


def hours_response(business: Business) -> str:
    """Build business hours response."""
    if not business.business_hours:
        return "Please contact us for our current operating hours."

    day_names = {
        "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
        "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
    }

    currently_open = is_business_open(business.business_hours, business.timezone)
    status = "✅ *We're currently OPEN!*" if currently_open else "🔴 *We're currently CLOSED*"

    lines = [f"🕐 *{business.name} Hours*\n", status, ""]

    for day_key in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        day_hours = business.business_hours.get(day_key)
        day_name = day_names[day_key]
        if day_hours:
            lines.append(f"  {day_name}: {day_hours['open']} - {day_hours['close']}")
        else:
            lines.append(f"  {day_name}: Closed")

    return "\n".join(lines)


def location_response(business: Business) -> str:
    """Build location/address response."""
    lines = [f"📍 *{business.name}*\n"]

    if business.address:
        lines.append(f"Address: {business.address}")
    if business.phone:
        lines.append(f"Phone: {business.phone}")

    if not business.address and not business.phone:
        lines.append("Contact us for location details.")

    return "\n".join(lines)


def closed_response(business: Business) -> str:
    """Response when business is closed."""
    if business.closed_text:
        return business.closed_text
    return (
        f"Sorry, *{business.name}* is currently closed. 🔴\n\n"
        'Send "hours" to see our operating hours.'
    )


def busy_response(business: Business) -> str:
    """Response when staff have paused WhatsApp ordering (too busy)."""
    if business.busy_text:
        return business.busy_text
    return (
        f"Sorry, *{business.name}* is currently too busy to take WhatsApp orders. 🙏\n\n"
        "Please place your order in store, or try again shortly."
    )


def fallback_response(business: Business) -> str:
    """Response when we can't understand the message."""
    if business.fallback_text:
        return business.fallback_text
    return (
        "Sorry, I didn't quite understand that. 🤔\n\n"
        "You can try:\n"
        '• "menu" — see our menu\n'
        '• "specials" — today\'s deals\n'
        '• "hours" — opening times\n'
        '• "order" — start an order\n'
        '• "help" — talk to a person'
    )


def opted_out_response() -> str:
    """Confirmation that opt-out was processed."""
    return (
        "You've been unsubscribed. ✅\n"
        'You won\'t receive any more messages from us.\n'
        'Send "START" anytime to re-subscribe.'
    )


def order_confirmation_response(
    order_number: str,
    cart_summary: str,
    total_cents: int,
    delivery_fee_cents: int = 0,
    order_mode: str = "PICKUP",
    currency: str = "ZAR",
) -> str:
    """Build the final order confirmation message."""
    lines = [
        "✅ *Order Confirmed!*\n",
        f"Order Number: *{order_number}*\n",
        cart_summary,
    ]

    if delivery_fee_cents > 0 and order_mode == "DELIVERY":
        lines.append(f"Delivery fee: {format_currency(delivery_fee_cents, currency)}")

    grand_total = total_cents + (delivery_fee_cents if order_mode == "DELIVERY" else 0)
    lines.append(f"\n💰 *Total: {format_currency(grand_total, currency)}*")
    lines.append(f"Mode: {'🛵 Delivery' if order_mode == 'DELIVERY' else '🏪 Pickup'}")
    lines.append("\nWe'll update you when your order status changes!")

    return "\n".join(lines)


def ask_confirmation_response(
    cart_summary: str,
    total_cents: int,
    delivery_fee_cents: int = 0,
    order_mode: str = "PICKUP",
    currency: str = "ZAR",
) -> str:
    """Ask the customer to confirm their order before placing it."""
    lines = [cart_summary]

    if delivery_fee_cents > 0 and order_mode == "DELIVERY":
        lines.append(f"Delivery fee: {format_currency(delivery_fee_cents, currency)}")

    grand_total = total_cents + (delivery_fee_cents if order_mode == "DELIVERY" else 0)
    lines.append(f"\n💰 *Total: {format_currency(grand_total, currency)}*")
    lines.append(f"\nReply *yes* to confirm or *no* to make changes.")

    return "\n".join(lines)


def collecting_details_response(
    need_name: bool,
    need_phone: bool,
    need_address: bool,
    already_have: dict,
) -> str:
    """Ask for missing customer details before order placement."""
    missing = []
    if need_name and "customer_name" not in already_have:
        missing.append("your *name*")
    if need_phone and "phone_number" not in already_have:
        missing.append("your *phone number*")
    if need_address and "delivery_address" not in already_have:
        missing.append("your *delivery address*")

    if not missing:
        return ""  # Nothing needed

    # Ask for one field at a time — avoids the customer trying to send
    # everything in one message, which breaks address/phone parsing and
    # can trigger unintended intent handlers (e.g. LOCATION_REQUEST).
    return f"Almost there! Please send me {missing[0]}."
