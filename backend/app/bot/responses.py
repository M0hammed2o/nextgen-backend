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


def specials_response(specials: list[Special], currency: str = "ZAR") -> str:
    """Build specials response, filtering to today's active specials."""
    if not specials:
        return "We don't have any specials running right now. Check back soon! 🤞"

    today_name = date.today().strftime("%a").lower()[:3]
    active_today = []

    for s in specials:
        if not s.is_active:
            continue
        # Check day-of-week filter
        if s.days_of_week and today_name not in s.days_of_week:
            continue
        # Check date range
        now = utc_now()
        if s.start_at and now < s.start_at:
            continue
        if s.end_at and now > s.end_at:
            continue
        active_today.append(s)

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

    if len(missing) == 1:
        return f"Almost there! Please send me {missing[0]}."
    else:
        items = ", ".join(missing[:-1]) + f" and {missing[-1]}"
        return f"Almost there! I need {items} to complete your order."
