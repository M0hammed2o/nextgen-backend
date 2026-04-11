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
    options: dict | None = None,
    special_instructions: str | None = None,
) -> list[dict]:
    """Add an item to the cart. Returns updated cart."""
    cart = get_cart(session)

    # Check if same item (with same options) already in cart
    for item in cart:
        if (item["menu_item_id"] == menu_item_id
                and item.get("options") == options):
            item["quantity"] += quantity
            item["line_total_cents"] = item["price_cents"] * item["quantity"]
            set_cart(session, cart)
            return cart

    cart.append({
        "menu_item_id": menu_item_id,
        "name": name,
        "price_cents": price_cents,
        "quantity": quantity,
        "line_total_cents": price_cents * quantity,
        "options": options,
        "special_instructions": special_instructions,
    })
    set_cart(session, cart)
    return cart


def remove_from_cart(session: ConversationSession, item_name: str) -> tuple[list[dict], bool]:
    """
    Remove an item from cart by name (fuzzy match).
    Returns (updated_cart, was_removed).
    """
    cart = get_cart(session)
    name_lower = item_name.lower().strip()

    for i, item in enumerate(cart):
        if name_lower in item["name"].lower():
            cart.pop(i)
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
        "pending_options", "recommended_items",
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
        if item.get("options"):
            for key, val in item["options"].items():
                lines.append(f"      ↳ {key}: {val}")
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
