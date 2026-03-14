"""
Order Creator — transactionally creates orders from confirmed carts.
Increments order number sequence, creates order + items, publishes to Redis.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.order import Order, OrderEvent, OrderItem
from shared.utils import format_order_number

logger = logging.getLogger("nextgen.bot.orders")


async def create_order_from_cart(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session: ConversationSession,
) -> Order:
    """
    Create an order from the cart in the conversation session.
    
    Transactional:
    1. Increment business.order_number_sequence
    2. Create order + order_items
    3. Create initial order_event
    4. Publish to Redis for SSE
    
    Raises ValueError if cart is empty.
    """
    ctx = session.context_json or {}
    cart = ctx.get("cart", [])

    if not cart:
        raise ValueError("Cannot create order: cart is empty")

    # ── 1. Increment order number atomically ─────────────────────────────
    result = await db.execute(
        update(Business)
        .where(Business.id == business.id)
        .values(order_number_sequence=Business.order_number_sequence + 1)
        .returning(Business.order_number_sequence)
    )
    seq = result.scalar_one()
    order_number = format_order_number(seq)

    # ── 2. Calculate totals ──────────────────────────────────────────────
    subtotal = sum(item["line_total_cents"] for item in cart)
    order_mode = ctx.get("order_mode", "PICKUP")
    delivery_fee = business.delivery_fee_cents if order_mode == "DELIVERY" else 0
    total = subtotal + delivery_fee

    # ── 3. Create order ──────────────────────────────────────────────────
    order = Order(
        business_id=business.id,
        customer_id=customer.id,
        order_number=order_number,
        status="NEW",
        order_mode=order_mode,
        source="WHATSAPP",
        subtotal_cents=subtotal,
        delivery_fee_cents=delivery_fee,
        total_cents=total,
        currency=business.currency,
        customer_name=ctx.get("customer_name") or customer.display_name,
        phone_number=ctx.get("phone_number") or customer.phone_number,
        delivery_address=ctx.get("delivery_address"),
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(order)
    await db.flush()  # Get order.id

    # ── 4. Create order items (snapshots) ────────────────────────────────
    for cart_item in cart:
        oi = OrderItem(
            order_id=order.id,
            business_id=business.id,
            menu_item_id=_parse_uuid(cart_item.get("menu_item_id")),
            name_snapshot=cart_item["name"],
            unit_price_cents=cart_item["price_cents"],
            quantity=cart_item["quantity"],
            line_total_cents=cart_item["line_total_cents"],
            options_snapshot=cart_item.get("options"),
            special_instructions=cart_item.get("special_instructions"),
        )
        db.add(oi)

    # ── 5. Create initial order event ────────────────────────────────────
    event = OrderEvent(
        order_id=order.id,
        business_id=business.id,
        old_status=None,
        new_status="NEW",
        reason="Order placed via WhatsApp",
    )
    db.add(event)

    await db.flush()

    # ── 6. Publish to Redis for SSE (fire and forget) ────────────────────
    try:
        from backend.app.db.session import get_redis
        redis = await get_redis()
        items_summary = ", ".join(
            f"{i['quantity']}x {i['name']}" for i in cart
        )
        await redis.publish(
            f"orders:{business.id}",
            json.dumps({
                "type": "order_created",
                "order_id": str(order.id),
                "order_number": order_number,
                "status": "NEW",
                "total_cents": total,
                "items_summary": items_summary,
                "customer_name": order.customer_name or "WhatsApp Customer",
                "order_mode": order_mode,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception:
        logger.warning("Failed to publish SSE event for new order %s", order_number)

    logger.info(
        "Order created: %s for business %s, total %d cents, %d items",
        order_number, business.id, total, len(cart),
    )

    return order


def _parse_uuid(val) -> uuid.UUID | None:
    """Safely parse a UUID from string or return None."""
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError):
        return None
