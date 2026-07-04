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
    pending_options: list[dict] | None = None,
    recommended_items: list[dict] | None = None,
) -> str:
    """
    Build a constrained system prompt for the LLM.
    Includes full menu, business rules, and current conversation context.
    """
    menu_text = _format_menu_for_prompt(categories, menu_items, business.currency)
    specials_text = _format_specials_for_prompt(specials)
    cart_text = _format_cart_for_prompt(cart, business.currency)
    pending_text = _format_pending_options_for_prompt(pending_options)
    recommended_text = _format_recommended_items_for_prompt(recommended_items)
    state_rules_text = _format_state_rules(conversation_state)

    return f"""You are the WhatsApp ordering assistant for {business.name}.
You help customers browse the menu, build orders, and answer questions.

═══ STRICT RULES (NEVER VIOLATE) ═══
1. ONLY quote prices from the menu below. NEVER invent or guess prices.
2. ONLY reference menu items that exist below. If an item isn't on the menu, say "Sorry, we don't have that on our menu."
3. NEVER invent business hours. If asked, say exactly: {_format_hours_for_prompt(business)}
4. You may describe well-known food/drink items naturally (e.g. what an iced coffee is, what a smash burger is) even if the menu description is blank — just never invent prices or claim items exist that aren't on the menu. For truly unknown business-specific questions (policies, allergies, opening exceptions), say "I'm not sure about that. Would you like me to connect you with our team?"
5. NEVER make promises about delivery times unless the business has specified them.
6. Be friendly, concise, and use WhatsApp formatting (*bold*, _italic_).
7. Keep responses under 300 words.
8. Use South African English naturally (but don't force slang).

═══ BUSINESS INFO ═══
Name: {business.name}
{f"Address: {business.address}" if business.address else ""}
{f"Phone: {business.phone}" if business.phone else ""}
Currency: {business.currency}
Delivery: {"Available (fee confirmed by staff per order)" if business.delivery_enabled else "Not available"}
Order mode: {"Dine-in/Pickup only" if business.order_in_only else "Pickup" + (" & Delivery" if business.delivery_enabled else "")}

═══ MENU ═══
{menu_text}

{f"═══ TODAY'S SPECIALS ═══{chr(10)}{specials_text}" if specials_text else ""}

═══ CURRENT CONVERSATION STATE ═══
State: {conversation_state}
{f"Current cart:{chr(10)}{cart_text}" if cart else "Cart: empty"}
{f"═══ PENDING ITEM (awaiting option choice) ═══{chr(10)}{pending_text}{chr(10)}The customer's next message answers the option question above. Resolve it and return add_items with the chosen option filled in." if pending_text else ""}
{f"═══ PREVIOUSLY RECOMMENDED ITEMS ═══{chr(10)}{recommended_text}{chr(10)}These items were recommended to the customer. If the customer now accepts (says yes/take those/I'll have that), use add_items for all of them. If they add more items too, include both recommended AND new items in add_items." if recommended_text else ""}

{f"═══ STATE-SPECIFIC RULES ═══{chr(10)}{state_rules_text}{chr(10)}" if state_rules_text else ""}═══ YOUR TASK ═══
Based on the customer's message, respond naturally AND output a JSON action block.

Your response MUST end with a JSON block on a new line in this exact format:
```json
{{"action": "<ACTION>", "items": [<ITEMS>], "message": "<YOUR_RESPONSE>"}}
```

ACTION must be one of:
- "add_items" — customer wants to add items. items = [{{"name": "exact menu item name", "quantity": 1, "options": {{}}, "add_ons": [], "special_instructions": ""}}]
  PAID ADD-ONS: When a customer requests an item listed under ✦ Add-ons for a menu item, capture
  it in the add_ons list: add_ons = [{{"name": "exact add-on name", "quantity": 1}}]
  These have a price — show the customer the correct total including the add-on cost.
  Examples: "burger with extra cheese"  → add_ons=[{{"name":"Extra Cheese","quantity":1}}]
            "2 extra patties"           → add_ons=[{{"name":"Extra Patty","quantity":2}}]
  FREE MODIFIERS: When a customer asks to modify an ingredient with NO listed add-on price
  ("no tomato", "without onion", "extra salt", "sauce on the side"), capture it in
  special_instructions. NEVER use remove_item for ingredient changes.
  Examples: "burger without tomato" → add_ons=[], special_instructions="no tomato"
            "chips with extra salt"  → add_ons=[], special_instructions="extra salt"
- "remove_item" — customer wants to remove an entire item from the cart (the whole menu item, e.g.
  "take off the burger", "I don't want the chips anymore", "cancel the pizza").
  items = [{{"name": "item to remove"}}]
  NEVER use remove_item when the customer only wants to remove an add-on or ingredient modifier.
  "remove the extra cheese" or "remove the soy milk" → use replace_item, NOT remove_item.
- "replace_item" — TWO uses:
  1. Swap one item for another: "change my Coke to a Sprite", "replace chips with cheesy chips"
  2. Modify add-ons or options on an existing cart item (remove/add/swap an add-on or milk choice)
  items = [{{"remove": "exact item name", "add": "exact item name", "quantity": 1,
             "options": {{}}, "add_ons": [<COMPLETE updated add-ons list>], "special_instructions": ""}}]
  ADD-ON MODIFICATION RULES:
  - Set "remove" and "add" to the SAME item name (the parent item stays in the cart).
  - "add_ons" must contain the COMPLETE final list of add-ons AFTER the change (not just the delta).
  - Always preserve any existing special_instructions that are not being changed.
  - Look at the Current Cart section above to see what add-ons the item already has.
  Examples (cart has: 1× Classic Smash Burger with ✦ Extra Cheese +R10):
    "remove extra cheese"
    → {{"remove":"Classic Smash Burger","add":"Classic Smash Burger","add_ons":[],"special_instructions":""}}
    "replace extra cheese with extra patty"
    → {{"remove":"Classic Smash Burger","add":"Classic Smash Burger","add_ons":[{{"name":"Extra Patty","quantity":1}}]}}
    "change soy milk to oat milk" (cart has: 1× Latte with ✦ Soy Milk)
    → {{"remove":"Latte","add":"Latte","options":{{"Milk":"Oat Milk"}},"add_ons":[]}}
    "change oat milk back to full cream"
    → {{"remove":"Latte","add":"Latte","options":{{"Milk":"Full Cream"}},"add_ons":[]}}
- "recommend_items" — you are recommending a specific item the customer should try.
  items = [{{"name": "exact menu item name", "quantity": 1, "options": {{}}, "special_instructions": ""}}]
  Recommend ONE item at a time in your message. Do NOT use "X or Y" phrasing — if you want to offer
  a choice between items, use "ask_options" instead. Items are NOT added to cart yet — customer must
  confirm first. ALWAYS use this action when recommending items, never chitchat.
- "confirm_order" — customer confirmed the order
- "cancel_order" — customer wants to cancel
- "ask_options" — need to clarify size/options before adding, OR customer needs to choose between alternatives
- "chitchat" — just responding to a question/greeting with NO ordering intent and NO item recommendations. NEVER use chitchat if you are recommending menu items.
- "handoff" — customer needs human help

IMPORTANT: The "name" field in items MUST exactly match a menu item name from the menu above.
For "replace_item", both "remove" and "add" must exactly match menu item names.
Use "replace_item" whenever the customer says: change X to Y, swap X for Y, instead of X give me Y.
Use "recommend_items" (never "chitchat") whenever you suggest specific menu items to a customer.
NEVER summarize or echo back cart contents in a chitchat message — the system builds all order summaries from the real cart.

OPTION HANDLING:
- Items may have option groups listed in the menu (marked ▸). Required groups MUST be collected.
- If the customer orders an item without specifying a REQUIRED option, use "ask_options". Ask for ONE group at a time. List the choices clearly.
- When the customer provides an option choice, capture it in special_instructions of the add_items entry (e.g. special_instructions="oat milk").
- NEVER ask about OPTIONAL option groups unless the customer brings them up.
- NEVER invent options that are not listed for the item.
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
        parts = [f"- {item.name} ({price})"]
        opts_text = _format_options_for_prompt(item.options_json, currency)
        if opts_text:
            parts.append(opts_text)
        item_add_ons = getattr(item, "add_ons", None) or []
        add_ons_text = _format_add_ons_for_prompt(item_add_ons, currency)
        if add_ons_text:
            parts.append(add_ons_text)
        items_list.append("\n".join(parts))

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


def _format_options_for_prompt(options_json: dict | None, currency: str = "ZAR") -> str:
    """
    Render option groups as compact, LLM-readable lines.
    Includes price_delta_cents so the LLM can quote accurate totals.
    Returns an empty string when there are no groups.
    """
    if not options_json:
        return ""
    groups = options_json.get("option_groups", [])
    if not groups:
        return ""

    from shared.utils.money import format_currency as _fmt

    lines = []
    for group in sorted(groups, key=lambda g: g.get("sort_order", 0)):
        if not group.get("is_enabled", True):
            continue
        req_label = "required" if group.get("required") else "optional"
        max_sel = group.get("max_selections", 1)
        choose = "choose 1" if max_sel == 1 else f"choose up to {max_sel}"
        choices = []
        for o in group.get("options", []):
            if not o.get("is_enabled", True):
                continue
            delta = o.get("price_delta_cents", 0)
            if delta > 0:
                choices.append(f"{o['name']} (+{_fmt(delta, currency)})")
            elif delta < 0:
                choices.append(f"{o['name']} ({_fmt(delta, currency)})")
            else:
                choices.append(f"{o['name']} (no charge)")
        lines.append(
            f"    ▸ {group['name']} ({req_label}, {choose}): "
            + " | ".join(choices)
        )
    return "\n".join(lines)


def _format_add_ons_for_prompt(add_ons: list, currency: str = "ZAR") -> str:
    """
    Render available paid add-ons for a menu item.
    Returns an empty string when there are no add-ons.
    """
    if not add_ons:
        return ""

    from shared.utils.money import format_currency as _fmt

    active = [a for a in add_ons if getattr(a, "is_active", True) and not getattr(a, "is_deleted", False)]
    if not active:
        return ""

    parts = []
    for ao in sorted(active, key=lambda a: getattr(a, "sort_order", 0)):
        name = getattr(ao, "name", ao.get("name", "") if isinstance(ao, dict) else "")
        price = getattr(ao, "price_cents", ao.get("price_cents", 0) if isinstance(ao, dict) else 0)
        max_q = getattr(ao, "max_qty", ao.get("max_qty", 10) if isinstance(ao, dict) else 10)
        limit = f" (max {max_q})" if max_q > 1 else ""
        parts.append(f"{name} +{_fmt(price, currency)}{limit}")

    return f"    ✦ Add-ons: " + " | ".join(parts)


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
        parts = [line]
        opts_text = _format_options_for_prompt(item.options_json, currency)
        if opts_text:
            parts.append(opts_text)
        # Add-ons may be a SQLAlchemy relationship list or a plain list on FakeMenuItem
        item_add_ons = getattr(item, "add_ons", None) or []
        add_ons_text = _format_add_ons_for_prompt(item_add_ons, currency)
        if add_ons_text:
            parts.append(add_ons_text)
        cat_map.setdefault(key, []).append("\n".join(parts))

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
    """
    Format current cart for the prompt context.
    Shows add-ons and free modifiers so the LLM knows exactly what's in the cart
    and can correctly apply replace_item when modifying add-ons.
    """
    if not cart:
        return ""
    lines = []
    for item in cart:
        price = format_currency(item["line_total_cents"], currency)
        lines.append(f"  {item['quantity']}x {item['name']} = {price}")
        for ao in (item.get("add_ons") or []):
            ao_qty = ao.get("quantity", 1)
            qty_str = f" ×{ao_qty}" if ao_qty > 1 else ""
            lines.append(f"      ✦ {ao['name']}{qty_str} (+{format_currency(ao['price_cents'] * ao_qty, currency)})")
        if item.get("special_instructions"):
            lines.append(f"      📝 {item['special_instructions']}")
    total = sum(i["line_total_cents"] for i in cart)
    lines.append(f"  Subtotal: {format_currency(total, currency)}")
    return "\n".join(lines)


def _format_recommended_items_for_prompt(recommended_items: list[dict] | None) -> str:
    """Format previously recommended items for system prompt context."""
    if not recommended_items:
        return ""
    lines = []
    for r in recommended_items:
        line = f"- {r['name']} (qty: {r.get('quantity', 1)})"
        lines.append(line)
    return "\n".join(lines)


def _format_pending_options_for_prompt(pending_options: list[dict] | None) -> str:
    """Format pending items waiting for option clarification, including available choices."""
    if not pending_options:
        return ""
    lines = []
    for p in pending_options:
        line = f"- {p['name']} (qty: {p.get('quantity', 1)})"
        if p.get("special_instructions"):
            line += f" — already noted: {p['special_instructions']}"
        lines.append(line)
        # Show available option groups so the LLM can resolve the customer's answer
        opts_json = p.get("options_json") or {}
        for group in sorted(
            opts_json.get("option_groups", []),
            key=lambda g: g.get("sort_order", 0),
        ):
            if not group.get("is_enabled", True):
                continue
            req_label = "REQUIRED" if group.get("required") else "optional"
            enabled = [
                o["name"] for o in group.get("options", [])
                if o.get("is_enabled", True)
            ]
            lines.append(
                f"  {group['name']} ({req_label}): " + " | ".join(enabled)
            )
    return "\n".join(lines)


def _format_state_rules(conversation_state: str) -> str:
    """Return extra state-specific instructions injected into the system prompt."""
    if conversation_state == "CHOOSING_OPTIONS":
        return (
            "The customer is answering a question about required options or sizes.\n"
            "Look at the PENDING ITEM section above to understand what they're choosing from.\n"
            "- Identify the option they chose from the listed choices.\n"
            "- Return add_items with that item, putting the chosen option in special_instructions.\n"
            "- If the customer is unsure or asks again, use ask_options to re-present the choices.\n"
            "- If the customer wants to cancel, use cancel_order.\n"
            "- Do NOT re-ask for an option the customer just provided.\n"
            "- Do NOT ask about optional groups — only resolve the required option."
        )
    if conversation_state == "CONFIRMING_ORDER":
        return (
            "The customer is currently reviewing their order (CONFIRMING_ORDER).\n"
            "- If they say YES / confirm → use \"confirm_order\"\n"
            "- If they say NO / cancel / don't want this / actually no / never mind / start over\n"
            "  → use \"cancel_order\". This clears the cart so the customer can start fresh.\n"
            "- If they want to ADD a NEW menu item (e.g. \"also give me chips\", \"add a Coke\") → use \"add_items\".\n"
            "- If they want to ADD a paid add-on to an EXISTING cart item (e.g. \"add extra patty\",\n"
            "  \"extra cheese please\", \"add bacon\") → use \"replace_item\" with remove=add=the existing item name\n"
            "  and add_ons containing the COMPLETE final list (existing add-ons + the new one).\n"
            "  Example: cart has 1× Burger (no add-ons). Customer says \"add extra patty\".\n"
            "  → {\"remove\":\"Loaded Smash Burger\",\"add\":\"Loaded Smash Burger\",\"add_ons\":[{\"name\":\"Extra Patty\",\"quantity\":1}]}\n"
            "- If they want to REMOVE a whole menu item → use \"remove_item\".\n"
            "  If they want to REMOVE a paid add-on → use \"replace_item\" (NEVER \"remove_item\" for add-ons).\n"
            "- If they want to SWAP or CHANGE something → use \"replace_item\".\n"
            "- NEVER use \"chitchat\" for add/remove/change/cancel requests — always use the correct action.\n"
            "- Never summarise the cart in a chitchat message — the system builds all order summaries."
        )
    return ""


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
