"""
LLM Prompt Builder — constructs system prompts with business context.

Key principles:
1. Never let the LLM hallucinate prices, hours, or menu items
2. Provide the FULL menu in the system prompt so LLM can reference it
3. Strict rules prevent the bot from making things up
4. Output format is constrained for reliable parsing
"""

import json

from shared.models.business import Business
from shared.models.menu import MenuCategory, MenuItem
from shared.models.specials import Special
from shared.utils.money import format_currency


def build_system_prompt(
    business: Business,
    categories: list[MenuCategory],
    menu_items: list[MenuItem],
    specials: list[Special],
    conversation_state: str,
    cart: list[dict],
) -> str:
    """
    Build a constrained system prompt for the LLM.
    Includes full menu, business rules, and current conversation context.
    """
    menu_text = _format_menu_for_prompt(categories, menu_items, business.currency)
    specials_text = _format_specials_for_prompt(specials)
    cart_text = _format_cart_for_prompt(cart, business.currency)

    return f"""You are the WhatsApp ordering assistant for {business.name}.
You help customers browse the menu, build orders, and answer questions.

═══ STRICT RULES (NEVER VIOLATE) ═══
1. ONLY quote prices from the menu below. NEVER invent or guess prices.
2. ONLY reference menu items that exist below. If an item isn't on the menu, say "Sorry, we don't have that on our menu."
3. NEVER invent business hours. If asked, say exactly: {_format_hours_for_prompt(business)}
4. If you don't know something, say "I'm not sure about that. Would you like me to connect you with our team?"
5. NEVER make promises about delivery times unless the business has specified them.
6. Be friendly, concise, and use WhatsApp formatting (*bold*, _italic_).
7. Keep responses under 300 words.
8. Use South African English naturally (but don't force slang).

═══ BUSINESS INFO ═══
Name: {business.name}
{f"Address: {business.address}" if business.address else ""}
{f"Phone: {business.phone}" if business.phone else ""}
Currency: {business.currency}
Delivery: {"Available (R{:.2f} delivery fee)".format(business.delivery_fee_cents / 100) if business.delivery_enabled else "Not available"}
Order mode: {"Dine-in/Pickup only" if business.order_in_only else "Pickup" + (" & Delivery" if business.delivery_enabled else "")}

═══ MENU ═══
{menu_text}

{f"═══ TODAY'S SPECIALS ═══{chr(10)}{specials_text}" if specials_text else ""}

═══ CURRENT CONVERSATION STATE ═══
State: {conversation_state}
{f"Current cart:{chr(10)}{cart_text}" if cart else "Cart: empty"}

═══ YOUR TASK ═══
Based on the customer's message, respond naturally AND output a JSON action block.

Your response MUST end with a JSON block on a new line in this exact format:
```json
{{"action": "<ACTION>", "items": [<ITEMS>], "message": "<YOUR_RESPONSE>"}}
```

ACTION must be one of:
- "add_items" — customer wants to add items. items = [{{"name": "exact menu item name", "quantity": 1, "options": {{}}, "special_instructions": ""}}]
- "remove_item" — customer wants to remove an item. items = [{{"name": "item to remove"}}]
- "replace_item" — customer wants to swap one item for another (e.g. "change my Coke 500ml to Coke 330ml", "replace chips with cheesy chips"). items = [{{"remove": "exact name of item to remove", "add": "exact name of new item", "quantity": 1, "options": {{}}, "special_instructions": ""}}]
- "confirm_order" — customer confirmed the order
- "cancel_order" — customer wants to cancel
- "ask_options" — need to clarify size/options before adding
- "chitchat" — just responding to a question/greeting, no cart changes
- "handoff" — customer needs human help

IMPORTANT: The "name" field in items MUST exactly match a menu item name from the menu above.
For "replace_item", both "remove" and "add" must exactly match menu item names.
Use "replace_item" whenever the customer says: change X to Y, swap X for Y, instead of X I want Y, upgrade X to Y.

═══ HANDLING POPULAR / RECOMMENDATION QUERIES ═══
When a customer asks "what is popular", "what do you recommend", "what's good here", "what's your best", or similar:
- Use action "chitchat" (no items array needed)
- In "message", list 3-5 real items from the menu above with their exact prices and descriptions
- Format each item as: "🍔 *Item Name* — R{price}\n_{description}_"
- End with a friendly question like "Would you like to order any of these? 😊"
- NEVER mention items not on the menu above
- NEVER invent or guess prices — use only the prices listed above
"""


def build_item_parsing_prompt(
    message: str,
    menu_items: list[MenuItem],
    currency: str = "ZAR",
) -> str:
    """
    Minimal prompt just for parsing items from a customer message.
    Used when the rules engine identified ORDER_START/ORDER_ADD intent
    but we need LLM to extract specific items + quantities.
    """
    items_list = []
    for item in menu_items:
        if not item.is_active or item.is_deleted:
            continue
        price = format_currency(item.price_cents, currency)
        entry = f"- {item.name} ({price})"
        if item.options_json:
            entry += f" [options: {json.dumps(item.options_json)}]"
        items_list.append(entry)

    menu_str = "\n".join(items_list)

    return f"""Extract the ordered items from the customer message below.

Available menu items:
{menu_str}

Customer message: "{message}"

Respond ONLY with a JSON array. Each element must have:
- "name": exact menu item name from the list above (must match exactly)
- "quantity": integer (default 1)
- "options": object (if customer specified size/flavor/etc)
- "special_instructions": string or null

If a requested item doesn't match any menu item, include it with "name": null and "original_text": "what they said".

Example: [{{"name": "Classic Beef Burger", "quantity": 2, "options": {{}}, "special_instructions": null}}]

JSON only, no other text:"""


def _format_menu_for_prompt(
    categories: list[MenuCategory],
    items: list[MenuItem],
    currency: str,
) -> str:
    """Format menu for the system prompt."""
    if not items:
        return "Menu is currently empty."

    cat_map: dict[str | None, list[str]] = {}
    cat_names: dict[str | None, str] = {}

    for cat in sorted(categories, key=lambda c: c.sort_order):
        cat_names[str(cat.id)] = cat.name

    for item in items:
        if not item.is_active or item.is_deleted:
            continue
        key = str(item.category_id) if item.category_id else None
        price = format_currency(item.price_cents, currency)
        line = f"  - {item.name}: {price}"
        if item.description:
            line += f" ({item.description})"
        if item.options_json:
            line += f" [Options: {json.dumps(item.options_json)}]"
        cat_map.setdefault(key, []).append(line)

    lines = []
    for cat in sorted(categories, key=lambda c: c.sort_order):
        key = str(cat.id)
        if key in cat_map:
            lines.append(f"{cat.name}:")
            lines.extend(cat_map[key])
    # Uncategorized
    if None in cat_map:
        lines.append("Other:")
        lines.extend(cat_map[None])

    return "\n".join(lines) if lines else "Menu is currently empty."


def _format_specials_for_prompt(specials: list[Special]) -> str:
    """Format active specials for the prompt."""
    if not specials:
        return ""
    lines = []
    for s in specials:
        if s.is_active:
            line = f"- {s.title}"
            if s.description:
                line += f": {s.description}"
            lines.append(line)
    return "\n".join(lines)


def _format_cart_for_prompt(cart: list[dict], currency: str) -> str:
    """Format current cart for the prompt context."""
    if not cart:
        return ""
    lines = []
    for item in cart:
        price = format_currency(item["line_total_cents"], currency)
        lines.append(f"  {item['quantity']}x {item['name']} = {price}")
    total = sum(i["line_total_cents"] for i in cart)
    lines.append(f"  Subtotal: {format_currency(total, currency)}")
    return "\n".join(lines)


def _format_hours_for_prompt(business: Business) -> str:
    """Format hours as a compact string for the system prompt."""
    if not business.business_hours:
        return '"Contact us for hours"'

    day_names = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
                 "fri": "Fri", "sat": "Sat", "sun": "Sun"}
    parts = []
    for day_key in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
        hours = business.business_hours.get(day_key)
        if hours:
            parts.append(f"{day_names[day_key]} {hours['open']}-{hours['close']}")
        else:
            parts.append(f"{day_names[day_key]} Closed")
    return ", ".join(parts)
