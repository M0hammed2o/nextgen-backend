"""
Conversation State Machine — manages session state and cart context.

States:
  IDLE → GREETING → BROWSING_MENU → BUILDING_CART → CHOOSING_OPTIONS
  → CONFIRMING_ORDER → COLLECTING_DETAILS → ORDER_PLACED → HANDOFF

context_json stores:
  {
      "cart": [{"menu_item_id": "...", "name": "...", "price_cents": 1500, "quantity": 2, ...}],
      "order_mode": "PICKUP",
      "customer_name": "...",
      "phone_number": "...",
      "delivery_address": "...",
      "pending_options": {"menu_item_id": "...", "options_needed": ["size"]},
      "last_bot_question": "What size would you like?",
  }
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from shared.enums import ConversationState
from shared.models.customer import ConversationSession

SESSION_TIMEOUT_MINUTES = 60

import logging
_sm_logger = logging.getLogger("nextgen.bot.state_machine")


async def get_or_create_session(
    db: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> ConversationSession:
    """
    Get the active conversation session for this customer+business,
    or create a new one. Auto-expires stale sessions.

    Race-safe: uses INSERT … ON CONFLICT DO NOTHING so that concurrent
    webhook deliveries cannot create duplicate session rows (relies on
    the uq_conversation_sessions_business_customer DB constraint).
    """
    now = datetime.now(timezone.utc)

    # Ensure exactly one row exists — no-op if row already present
    await db.execute(
        pg_insert(ConversationSession)
        .values(
            business_id=business_id,
            customer_id=customer_id,
            state=ConversationState.IDLE.value,
            context_json={},
            last_activity_at=now,
            expires_at=now + timedelta(minutes=SESSION_TIMEOUT_MINUTES),
        )
        .on_conflict_do_nothing(
            constraint="uq_conversation_sessions_business_customer"
        )
    )

    result = await db.execute(
        select(ConversationSession).where(
            ConversationSession.business_id == business_id,
            ConversationSession.customer_id == customer_id,
        )
    )
    session = result.scalar_one()

    # Reset expired session
    if session.expires_at and session.expires_at < now:
        session.state = ConversationState.IDLE.value
        session.context_json = {}

    session.last_activity_at = now
    session.expires_at = now + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
    await db.flush()

    ctx = session.context_json or {}
    _sm_logger.warning(
        "SESSION_LOAD: session_id=%s, state=%s, cart_items=%d, confirmed_cart_items=%d",
        session.id,
        session.state,
        len(ctx.get("cart", [])),
        len(ctx.get("confirmed_cart", [])),
    )
    return session


def get_cart(session: ConversationSession) -> list[dict]:
    """Get the cart from session context."""
    ctx = session.context_json or {}
    return ctx.get("cart", [])


def set_cart(session: ConversationSession, cart: list[dict]) -> None:
    """Set the cart in session context. Uses a new dict + flag_modified so
    SQLAlchemy always detects the JSONB change regardless of object identity."""
    ctx = dict(session.context_json or {})
    ctx["cart"] = cart
    session.context_json = ctx
    flag_modified(session, "context_json")


def add_to_cart(
    session: ConversationSession,
    menu_item_id: str,
    name: str,
    price_cents: int,
    quantity: int = 1,
    selected_options: list[dict] | None = None,
    add_ons: list[dict] | None = None,
    options: dict | None = None,
    special_instructions: str | None = None,
) -> list[dict]:
    """Add an item to the cart. Returns updated cart.

    Phase 8: accepts selected_options (with price_delta_cents) and add_ons
    (with price_cents per unit). The effective unit price is computed via the
    pricing engine. All callers without these new params behave identically to
    before — price_cents remains the base price, unit_price = base price.

    Matching: items are accumulated (quantity++) only when menu_item_id,
    selected_options, add_ons, and special_instructions all match. Different
    option/add-on combinations are separate line items.
    """
    from shared.pricing.engine import calculate_line_item

    _sel_opts = selected_options or []
    _add_ons = add_ons or []

    breakdown = calculate_line_item(price_cents, _sel_opts, _add_ons, quantity)
    unit_price = breakdown.unit_price_cents

    cart = get_cart(session)

    # Match on all fields that make two cart entries distinct.
    # (item.get("selected_options") or []) handles old cart items without the field.
    for item in cart:
        if (
            item["menu_item_id"] == menu_item_id
            and (item.get("selected_options") or []) == _sel_opts
            and (item.get("add_ons") or []) == _add_ons
            and item.get("special_instructions") == special_instructions
        ):
            item["quantity"] += quantity
            item["line_total_cents"] = item["price_cents"] * item["quantity"]
            set_cart(session, cart)
            return cart

    cart.append({
        "menu_item_id": menu_item_id,
        "name": name,
        # price_cents = unit_price so that all legacy callers reading
        # item["price_cents"] for line-total recalculation stay correct.
        "price_cents": unit_price,
        "base_price_cents": price_cents,
        "selected_options": _sel_opts,
        "add_ons": _add_ons,
        "option_adjustment_cents": breakdown.option_adjustment_cents,
        "add_on_total_cents": breakdown.add_on_total_cents,
        "unit_price_cents": unit_price,
        "quantity": quantity,
        "line_total_cents": breakdown.line_total_cents,
        "options": options,
        "special_instructions": special_instructions,
    })
    set_cart(session, cart)
    return cart


def remove_from_cart(
    session: ConversationSession,
    item_name: str,
    quantity: int | None = None,
    qualifier_hint: str | None = None,
) -> tuple[list[dict], bool]:
    """
    Remove or reduce an item from cart by name (fuzzy match).

    `quantity`       — if provided and item.quantity > quantity, reduce by that
                       amount instead of removing the entry entirely.
    `qualifier_hint` — customer's original message used to disambiguate when
                       multiple items share the same name (e.g. two burgers,
                       one plain and one with no tomato).

    Disambiguation rules (applied only when multiple same-name matches exist):
      • "plain", "normal", "original", "regular", "without" in the hint
        → prefer the item whose special_instructions is empty/None
      • Specific ingredient word in the hint (e.g. "tomato", "cheese")
        → prefer the item whose special_instructions contains that word
      • No distinguishing hint → remove the first match (existing behaviour)

    Returns (updated_cart, was_found).
    """
    cart = get_cart(session)
    name_lower = item_name.lower().strip()

    candidates = [
        (i, item) for i, item in enumerate(cart)
        if name_lower in item["name"].lower()
    ]

    if not candidates:
        return cart, False

    # Choose which candidate to remove / reduce
    target_idx, target_item = candidates[0]   # default: first match

    if len(candidates) > 1 and qualifier_hint:
        hint = qualifier_hint.lower()

        # Words signalling the customer wants the UNMODIFIED item
        _PLAIN_WORDS = {"plain", "normal", "original", "regular", "without"}
        wants_plain = any(w in hint.split() for w in _PLAIN_WORDS)

        if wants_plain:
            for idx, item in candidates:
                if not item.get("special_instructions"):
                    target_idx, target_item = idx, item
                    break
        else:
            # Look for a specific ingredient word in the hint that matches
            # one of the candidates' special_instructions
            for idx, item in candidates:
                instr = (item.get("special_instructions") or "").lower()
                if instr and any(word in hint for word in instr.split()):
                    target_idx, target_item = idx, item
                    break

    # Apply removal / reduction
    if quantity is not None and target_item["quantity"] > quantity:
        target_item["quantity"] -= quantity
        target_item["line_total_cents"] = target_item["price_cents"] * target_item["quantity"]
        set_cart(session, cart)
        return cart, True

    cart.pop(target_idx)
    set_cart(session, cart)
    return cart, True


def update_cart_item_instructions(
    session: ConversationSession,
    item_name: str,
    special_instructions: str,
) -> tuple[list[dict], bool]:
    """
    Merge new special_instructions into an existing cart item (fuzzy name match).

    Appends to any existing instructions with ", " so that successive modifier
    messages ("no tomato" then "extra cheese") accumulate correctly.
    Returns (updated_cart, was_found).
    """
    cart = get_cart(session)
    name_lower = item_name.lower().strip()

    for item in cart:
        item_name_lower = item["name"].lower()
        if name_lower in item_name_lower or item_name_lower in name_lower:
            existing = item.get("special_instructions") or ""
            if existing:
                item["special_instructions"] = existing + ", " + special_instructions
            else:
                item["special_instructions"] = special_instructions
            set_cart(session, cart)
            return cart, True

    return cart, False


def remove_modifier_from_instructions(
    session: ConversationSession,
    item_name: str,
    modifier_word: str,
) -> tuple[list[dict], bool]:
    """
    Remove a modifier phrase containing *modifier_word* from a cart item's
    special_instructions.  Used when the customer says "actually leave the tomato"
    after a "no tomato" was set.

    The modifier_word should be the ingredient (e.g. "tomato"), not the full
    "no tomato" string — the function strips any clause that contains that word.

    Returns (updated_cart, was_updated).
    """
    import re as _re
    cart = get_cart(session)
    name_lower = item_name.lower().strip()
    word_lower = modifier_word.lower().strip()

    for item in cart:
        if name_lower not in item["name"].lower() and item["name"].lower() not in name_lower:
            continue
        existing = item.get("special_instructions") or ""
        if not existing:
            return cart, False
        # Split on comma, drop any clause that contains the ingredient word
        clauses = [c.strip() for c in existing.split(",")]
        new_clauses = [c for c in clauses if word_lower not in c.lower()]
        new_instr = ", ".join(new_clauses) if new_clauses else None
        if new_instr == existing:
            return cart, False
        item["special_instructions"] = new_instr
        set_cart(session, cart)
        return cart, True

    return cart, False


def remove_addon_from_cart_item(
    session: ConversationSession,
    item_name: str,
    addon_name: str,
) -> tuple[list[dict], bool]:
    """
    Remove a specific paid add-on from a cart item by name (fuzzy match on both).
    Reprices the cart item after removal.

    Returns (updated_cart, was_found).
    """
    from shared.pricing.engine import calculate_line_item

    cart = get_cart(session)
    item_name_lower = item_name.lower().strip()
    addon_name_lower = addon_name.lower().strip()

    for item in cart:
        cart_name_lower = item["name"].lower()
        if item_name_lower not in cart_name_lower and cart_name_lower not in item_name_lower:
            continue
        current_addons = list(item.get("add_ons") or [])
        new_addons = [ao for ao in current_addons if addon_name_lower not in ao["name"].lower()]
        if len(new_addons) == len(current_addons):
            return cart, False  # add-on not present on this item
        item["add_ons"] = new_addons
        breakdown = calculate_line_item(
            item.get("base_price_cents") or item["price_cents"],
            item.get("selected_options") or [],
            new_addons,
            item["quantity"],
        )
        item["price_cents"] = breakdown.unit_price_cents
        item["unit_price_cents"] = breakdown.unit_price_cents
        item["add_on_total_cents"] = breakdown.add_on_total_cents
        item["line_total_cents"] = breakdown.line_total_cents
        set_cart(session, cart)
        return cart, True

    return cart, False


def add_addon_to_cart_item(
    session: ConversationSession,
    item_name: str,
    addon: dict,
) -> tuple[list[dict], bool]:
    """
    Add a specific paid add-on to a cart item (fuzzy match on item name).
    Skips duplicates (by name). Reprices after addition.

    addon must contain: {"add_on_id", "name", "price_cents", "quantity"}
    Returns (updated_cart, was_found).
    """
    from shared.pricing.engine import calculate_line_item

    cart = get_cart(session)
    item_name_lower = item_name.lower().strip()
    addon_name_lower = addon["name"].lower()

    for item in cart:
        cart_name_lower = item["name"].lower()
        if item_name_lower not in cart_name_lower and cart_name_lower not in item_name_lower:
            continue
        current_addons = list(item.get("add_ons") or [])
        if any(ao["name"].lower() == addon_name_lower for ao in current_addons):
            return cart, True  # already present — no-op
        current_addons.append(addon)
        item["add_ons"] = current_addons
        breakdown = calculate_line_item(
            item.get("base_price_cents") or item["price_cents"],
            item.get("selected_options") or [],
            current_addons,
            item["quantity"],
        )
        item["price_cents"] = breakdown.unit_price_cents
        item["unit_price_cents"] = breakdown.unit_price_cents
        item["add_on_total_cents"] = breakdown.add_on_total_cents
        item["line_total_cents"] = breakdown.line_total_cents
        set_cart(session, cart)
        return cart, True

    return cart, False


def clear_cart(session: ConversationSession) -> None:
    """Clear cart, confirmed_cart, and all order-specific context so the next
    order starts completely fresh. Customer name/phone are kept to avoid
    re-collecting them on repeat orders."""
    ctx = dict(session.context_json or {})
    ctx["cart"] = []
    for key in (
        "confirmed_cart", "order_mode", "pending_order_id",
        "delivery_fee_cents", "delivery_fee_status", "payment_method",
        "pending_options", "proposed_items", "recommended_items", "superseded_order_id",
    ):
        ctx.pop(key, None)
    session.context_json = ctx
    flag_modified(session, "context_json")


def cart_total_cents(session: ConversationSession) -> int:
    """Calculate cart subtotal in cents."""
    return sum(item["line_total_cents"] for item in get_cart(session))


def cart_summary_text(session: ConversationSession, currency: str = "ZAR") -> str:
    """
    Build a human-readable cart summary for WhatsApp.
    Example:
        🛒 *Your Order:*
        2x Classic Beef Burger — R170.00
        1x Chips (Regular) — R35.00
        ─────────────
        *Subtotal: R205.00*
    """
    from shared.utils.money import format_currency
    cart = get_cart(session)
    if not cart:
        return "Your cart is empty."

    lines = ["🛒 *Your Order:*"]
    for item in cart:
        price_str = format_currency(item["line_total_cents"], currency)
        lines.append(f"  {item['quantity']}x {item['name']} — {price_str}")

        # Show priced option selections (e.g. "Oat Milk +R10.00")
        _sel_opts_shown: set[str] = set()
        for opt in (item.get("selected_options") or []):
            opt_name = opt.get("option_name", "")
            delta = opt.get("price_delta_cents", 0)
            if delta != 0:
                sign = "+" if delta > 0 else ""
                lines.append(f"      ↳ {opt_name} {sign}{format_currency(delta, currency)}")
            else:
                lines.append(f"      ↳ {opt_name}")
            _sel_opts_shown.add(opt_name.lower())

        # Fall back to legacy options dict for items stored before Phase 8
        if item.get("options") and not item.get("selected_options"):
            for key, val in item["options"].items():
                lines.append(f"      ↳ {key}: {val}")

        # Show paid add-ons with price breakdown
        for ao in (item.get("add_ons") or []):
            ao_qty = ao.get("quantity", 1)
            ao_total = ao.get("price_cents", 0) * ao_qty
            qty_str = f"×{ao_qty}" if ao_qty > 1 else ""
            lines.append(f"      ✦ {ao['name']}{qty_str} +{format_currency(ao_total, currency)}")

        if item.get("special_instructions"):
            lines.append(f"      📝 {item['special_instructions']}")

    lines.append("─────────────")
    total = cart_total_cents(session)
    lines.append(f"*Subtotal: {format_currency(total, currency)}*")

    return "\n".join(lines)


def set_context(session: ConversationSession, key: str, value) -> None:
    """Set a value in session context. Uses a new dict + flag_modified so
    SQLAlchemy always detects the JSONB change."""
    ctx = dict(session.context_json or {})
    ctx[key] = value
    session.context_json = ctx
    flag_modified(session, "context_json")


def get_context(session: ConversationSession, key: str, default=None):
    """Get a value from session context."""
    ctx = session.context_json or {}
    return ctx.get(key, default)


def transition_state(session: ConversationSession, new_state: str) -> str:
    """Transition the session to a new state. Returns the new state."""
    old = session.state
    session.state = new_state
    return old
