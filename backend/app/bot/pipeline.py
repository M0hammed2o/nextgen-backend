"""
Message Processing Pipeline — the heart of the bot.

Inbound message → full processing → outbound response.

Pipeline steps:
1. Resolve business (by phone_number_id)
2. Check business active + not suspended
3. Get/create customer (by wa_id)
4. Deduplicate (wa_message_id)
5. Check opt-out
6. Persist inbound message
7. Check daily message limit
8. Load/init conversation session
9. Check business hours
10. Run rules engine (keyword → intent)
11. If needed: call LLM
12. Process intent → update state → build response
13. If order confirmed: create order + publish SSE
14. Send outbound message (outbox pattern)
15. Update daily usage
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.bot import (
    intent_router,
    llm_parser,
    order_creator,
    prompt_builder,
    responses,
    state_machine,
    usage_tracker,
    whatsapp_sender,
)
from backend.app.core.errors import DailyLimitError
from shared.enums import ConversationState, MessageIntent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from shared.models.specials import Special
from shared.utils.time import is_business_open

logger = logging.getLogger("nextgen.bot.pipeline")


async def process_inbound_message(
    db: AsyncSession,
    phone_number_id: str,
    wa_message_id: str,
    wa_id: str,
    msg_text: str,
    msg_type: str,
    raw_payload: dict,
    contact_name: str | None = None,
) -> None:
    """
    Full message processing pipeline.
    Always commits at the end (or on error).
    """
    try:
        await _process(
            db, phone_number_id, wa_message_id, wa_id,
            msg_text, msg_type, raw_payload, contact_name,
        )
        await db.commit()
        logger.info("PIPELINE_COMMITTED: wa_message_id=%s", wa_message_id)
    except Exception:
        logger.exception(
            "PIPELINE_EXCEPTION: wa_message_id=%s, phone_number_id=%s — rolling back",
            wa_message_id, phone_number_id,
        )
        await db.rollback()


async def _process(
    db: AsyncSession,
    phone_number_id: str,
    wa_message_id: str,
    wa_id: str,
    msg_text: str,
    msg_type: str,
    raw_payload: dict,
    contact_name: str | None,
) -> None:
    logger.info(
        "PIPELINE_START: wa_message_id=%s, phone_number_id=%s, wa_id=%s, type=%s",
        wa_message_id, phone_number_id, wa_id, msg_type,
    )

    # ── 1. Resolve business ──────────────────────────────────────────────
    result = await db.execute(
        select(Business).where(Business.whatsapp_phone_number_id == phone_number_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        logger.warning(
            "PIPELINE_NO_BUSINESS: phone_number_id=%s — no business registered for this number. "
            "Check that the business record has whatsapp_phone_number_id=%s set in the DB.",
            phone_number_id, phone_number_id,
        )
        return

    logger.info(
        "PIPELINE_BUSINESS_FOUND: business_id=%s, name=%s, active=%s, whatsapp_enabled=%s",
        business.id, business.name, business.is_active,
        getattr(business, "is_whatsapp_enabled", True),
    )

    # ── 2. Check active ──────────────────────────────────────────────────
    if not business.is_active:
        logger.info("PIPELINE_BUSINESS_INACTIVE: business_id=%s, skipping", business.id)
        return

    # ── 3. Get/create customer ───────────────────────────────────────────
    customer = await _get_or_create_customer(db, business.id, wa_id, contact_name)
    logger.info(
        "PIPELINE_CUSTOMER: customer_id=%s, opted_out=%s, wa_id=%s",
        customer.id, customer.opted_out, wa_id,
    )

    # ── 4. Idempotency ───────────────────────────────────────────────────
    existing = await db.execute(
        select(Message.id).where(Message.wa_message_id == wa_message_id)
    )
    if existing.scalar_one_or_none():
        logger.info("PIPELINE_DUPLICATE: wa_message_id=%s already processed, skipping", wa_message_id)
        return

    # ── 5. Check opt-out ─────────────────────────────────────────────────
    if customer.opted_out:
        logger.info("PIPELINE_OPTED_OUT: customer_id=%s, wa_id=%s, skipping", customer.id, wa_id)
        return

    # ── 6. Detect opt-out intent early ───────────────────────────────────
    intent = intent_router.match_intent(msg_text)
    if intent == MessageIntent.OPT_OUT:
        customer.opted_out = True
        inbound = _persist_inbound(db, business.id, customer.id, wa_message_id, msg_text, raw_payload, "OPT_OUT")
        await _send_response(db, business, customer, wa_id, responses.opted_out_response(), intent="OPT_OUT")
        await usage_tracker.increment_usage(db, business.id, business.timezone, inbound_messages=1, outbound_messages=1)
        return

    # ── 7. Persist inbound message ───────────────────────────────────────
    inbound = _persist_inbound(
        db, business.id, customer.id, wa_message_id, msg_text, raw_payload,
        intent.value if intent else None,
    )

    # ── 8. Check daily message limit ─────────────────────────────────────
    try:
        await usage_tracker.check_daily_limit(db, business, "messages")
    except DailyLimitError:
        logger.warning("Business %s hit daily message limit", business.id)
        return  # Silently drop — don't tell customer about internal limits

    # ── 9. Load conversation session ─────────────────────────────────────
    session = await state_machine.get_or_create_session(db, business.id, customer.id)

    # ── 10. Check business hours ─────────────────────────────────────────
    if not is_business_open(business.business_hours or {}, business.timezone):
        # Allow info requests even when closed
        if intent not in (MessageIntent.HOURS_REQUEST, MessageIntent.LOCATION_REQUEST):
            response_text = responses.closed_response(business)
            await _send_response(db, business, customer, wa_id, response_text, intent="CLOSED")
            await usage_tracker.increment_usage(db, business.id, business.timezone, inbound_messages=1, outbound_messages=1)
            return

    # ── 11. Process based on intent + state ──────────────────────────────
    response_text, is_llm, llm_tokens, llm_cost, llm_provider = await _handle_message(
        db, business, customer, session, msg_text, intent,
    )

    # ── 12. Send response ────────────────────────────────────────────────
    if response_text:
        logger.info(
            "PIPELINE_SENDING_REPLY: business_id=%s, wa_id=%s, intent=%s, is_llm=%s, text_len=%d",
            business.id, wa_id, intent.value if intent else "UNKNOWN", is_llm, len(response_text),
        )
        await _send_response(
            db, business, customer, wa_id, response_text,
            is_llm=is_llm, llm_tokens=llm_tokens,
            llm_cost_cents=llm_cost, llm_provider=llm_provider,
            intent=intent.value if intent else "UNKNOWN",
        )
    else:
        logger.info(
            "PIPELINE_NO_REPLY: business_id=%s, wa_id=%s, intent=%s — no response_text generated",
            business.id, wa_id, intent.value if intent else "UNKNOWN",
        )

    # ── 13. Update usage ─────────────────────────────────────────────────
    await usage_tracker.increment_usage(
        db, business.id, business.timezone,
        inbound_messages=1,
        outbound_messages=1 if response_text else 0,
        llm_calls=1 if is_llm else 0,
        llm_tokens=llm_tokens or 0,
        llm_cost_cents=llm_cost or 0,
    )


async def _handle_message(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
    intent: MessageIntent | None,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    Handle the message based on intent and conversation state.
    Returns (response_text, is_llm, llm_tokens, llm_cost_cents, llm_provider).
    """
    current_state = session.state

    # ── Template responses (no LLM needed) ───────────────────────────────

    if intent == MessageIntent.GREETING:
        state_machine.transition_state(session, ConversationState.GREETING.value)
        return responses.greeting_response(business), False, None, None, None

    if intent == MessageIntent.MENU_REQUEST:
        categories, items = await _load_menu(db, business.id)
        state_machine.transition_state(session, ConversationState.BROWSING_MENU.value)
        return responses.menu_response(categories, items, business.currency), False, None, None, None

    if intent == MessageIntent.SPECIALS_REQUEST:
        specials = await _load_specials(db, business.id)
        return responses.specials_response(specials, business.currency), False, None, None, None

    if intent == MessageIntent.HOURS_REQUEST:
        return responses.hours_response(business), False, None, None, None

    if intent == MessageIntent.LOCATION_REQUEST:
        return responses.location_response(business), False, None, None, None

    if intent == MessageIntent.ORDER_CANCEL:
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return "Order cancelled. Your cart has been cleared. 🗑️\nAnything else I can help with?", False, None, None, None

    if intent == MessageIntent.ORDER_TRACK:
        # Check last order for this customer
        last_order = await _get_last_order(db, business.id, customer.id)
        if last_order:
            from shared.utils.money import format_currency
            return (
                f"📦 *Order {last_order.order_number}*\n"
                f"Status: *{last_order.status}*\n"
                f"Total: {format_currency(last_order.total_cents, business.currency)}\n"
                f"Placed: {last_order.created_at.strftime('%H:%M')}"
            ), False, None, None, None
        return "I couldn't find a recent order. Please check your order number.", False, None, None, None

    # ── Order confirmation (in CONFIRMING_ORDER state) ───────────────────
    if current_state == ConversationState.CONFIRMING_ORDER.value:
        if intent_router.is_confirmation(msg_text):
            return await _handle_order_confirmation(db, business, customer, session)
        elif intent_router.is_negation(msg_text):
            state_machine.transition_state(session, ConversationState.BUILDING_CART.value)
            return "No problem! What would you like to change?\n• Add more items\n• Remove something\n• Cancel the order", False, None, None, None

    # ── Collecting details state ─────────────────────────────────────────
    if current_state == ConversationState.COLLECTING_DETAILS.value:
        return await _handle_collecting_details(db, business, customer, session, msg_text)

    # ── LLM-required intents (ordering, ambiguous) ───────────────────────
    if intent_router.needs_llm(intent, current_state):
        try:
            await usage_tracker.check_daily_limit(db, business, "llm_calls")
        except DailyLimitError:
            return responses.fallback_response(business), False, None, None, None

        return await _handle_with_llm(db, business, customer, session, msg_text)

    # ── Fallback ─────────────────────────────────────────────────────────
    return responses.fallback_response(business), False, None, None, None


async def _handle_with_llm(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Call LLM, parse response, update cart/state accordingly."""
    categories, items = await _load_menu(db, business.id)
    specials = await _load_specials(db, business.id)
    cart = state_machine.get_cart(session)

    # Build prompt
    system_prompt = prompt_builder.build_system_prompt(
        business, categories, items, specials,
        session.state, cart,
    )

    # Call LLM
    from backend.app.llm.provider import get_llm_provider
    provider = get_llm_provider()
    llm_response = await provider.complete(system_prompt, msg_text)

    # Parse response
    parsed = llm_parser.parse_llm_response(llm_response.text)

    # Process action
    if parsed.action == "add_items" and parsed.items:
        # Match items to menu and add to cart
        items_map = {i.name.lower(): i for i in items if i.is_active and not i.is_deleted}
        added = []
        unmatched = []

        for pi in parsed.items:
            if not pi.name:
                if pi.original_text:
                    unmatched.append(pi.original_text)
                continue

            # Fuzzy match against menu
            matched_item = items_map.get(pi.name.lower())
            if not matched_item:
                # Try partial match
                for menu_name, menu_item in items_map.items():
                    if pi.name.lower() in menu_name or menu_name in pi.name.lower():
                        matched_item = menu_item
                        break

            if matched_item:
                state_machine.add_to_cart(
                    session,
                    menu_item_id=str(matched_item.id),
                    name=matched_item.name,
                    price_cents=matched_item.price_cents,
                    quantity=pi.quantity,
                    options=pi.options if pi.options else None,
                    special_instructions=pi.special_instructions,
                )
                added.append(f"{pi.quantity}x {matched_item.name}")
            else:
                unmatched.append(pi.name)

        state_machine.transition_state(session, ConversationState.BUILDING_CART.value)

        # Build response
        response_parts = []
        if added:
            response_parts.append("Added to your order: " + ", ".join(added) + " ✅")
        if unmatched:
            response_parts.append(f"Sorry, I couldn't find: {', '.join(unmatched)}. Check our menu for available items.")

        response_parts.append("\n" + state_machine.cart_summary_text(session, business.currency))
        response_parts.append('\nAnything else? Or say *"done"* to confirm your order.')

        return "\n".join(response_parts), True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

    elif parsed.action == "remove_item" and parsed.items:
        for pi in parsed.items:
            if pi.name:
                state_machine.remove_from_cart(session, pi.name)
        cart = state_machine.get_cart(session)
        if cart:
            msg = "Item removed.\n" + state_machine.cart_summary_text(session, business.currency)
            msg += '\nAnything else? Or say *"done"* to confirm.'
        else:
            msg = "Your cart is now empty. What would you like to order?"
            state_machine.transition_state(session, ConversationState.IDLE.value)
        return msg, True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

    elif parsed.action == "confirm_order":
        # Move to confirmation state
        cart = state_machine.get_cart(session)
        if not cart:
            return "Your cart is empty. What would you like to order?", True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        summary = state_machine.cart_summary_text(session, business.currency)
        total = state_machine.cart_total_cents(session)
        return responses.ask_confirmation_response(
            summary, total,
            business.delivery_fee_cents if state_machine.get_context(session, "order_mode") == "DELIVERY" else 0,
            state_machine.get_context(session, "order_mode", "PICKUP"),
            business.currency,
        ), True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

    elif parsed.action == "cancel_order":
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return "Order cancelled. 🗑️ Anything else?", True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

    elif parsed.action == "handoff":
        state_machine.transition_state(session, ConversationState.HANDOFF.value)
        return (
            "Let me connect you with our team. 👋\n"
            "A staff member will assist you shortly. Please hang tight!"
        ), True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider

    else:
        # chitchat / ask_options — return LLM's message directly
        return parsed.message, True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider


async def _handle_order_confirmation(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Handle order confirmation — check details, create order."""
    # Check if we need customer details
    already_have = {}
    if state_machine.get_context(session, "customer_name"):
        already_have["customer_name"] = True
    if state_machine.get_context(session, "phone_number") or customer.phone_number:
        already_have["phone_number"] = True
    if state_machine.get_context(session, "delivery_address"):
        already_have["delivery_address"] = True

    order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
    need_name = business.require_customer_name
    need_phone = business.require_phone_number
    need_address = business.require_delivery_address and order_mode == "DELIVERY"

    details_prompt = responses.collecting_details_response(
        need_name, need_phone, need_address, already_have
    )

    if details_prompt:
        state_machine.transition_state(session, ConversationState.COLLECTING_DETAILS.value)
        return details_prompt, False, None, None, None

    # All details collected — create order
    try:
        await usage_tracker.check_daily_limit(db, business, "orders")
    except DailyLimitError:
        return "Sorry, we can't accept more orders right now. Please try again later.", False, None, None, None

    order = await order_creator.create_order_from_cart(db, business, customer, session)

    # Build confirmation message
    summary = state_machine.cart_summary_text(session, business.currency)
    response_text = responses.order_confirmation_response(
        order.order_number, summary, order.subtotal_cents,
        order.delivery_fee_cents, order.order_mode, business.currency,
    )

    # Update usage
    await usage_tracker.increment_usage(
        db, business.id, business.timezone,
        orders_created=1, revenue_cents=order.total_cents,
    )

    # Reset session
    state_machine.clear_cart(session)
    state_machine.transition_state(session, ConversationState.ORDER_PLACED.value)

    return response_text, False, None, None, None


async def _handle_collecting_details(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Parse customer details from free-text message during COLLECTING_DETAILS state."""
    ctx = session.context_json or {}
    text = msg_text.strip()

    # Simple heuristic: check what we're still missing
    if business.require_customer_name and "customer_name" not in ctx:
        state_machine.set_context(session, "customer_name", text)
        # Also update customer record
        customer.display_name = text
        # Check if we need more
        still_missing = _check_missing_details(business, session)
        if still_missing:
            return still_missing, False, None, None, None
        # Proceed to create order
        return await _handle_order_confirmation(db, business, customer, session)

    if business.require_phone_number and "phone_number" not in ctx:
        # Basic phone validation
        import re
        phone = re.sub(r"[^\d+]", "", text)
        if len(phone) >= 9:
            state_machine.set_context(session, "phone_number", phone)
            customer.phone_number = phone
            still_missing = _check_missing_details(business, session)
            if still_missing:
                return still_missing, False, None, None, None
            return await _handle_order_confirmation(db, business, customer, session)
        return "Please send a valid phone number (e.g., 0812345678).", False, None, None, None

    if business.require_delivery_address and "delivery_address" not in ctx:
        state_machine.set_context(session, "delivery_address", text)
        still_missing = _check_missing_details(business, session)
        if still_missing:
            return still_missing, False, None, None, None
        return await _handle_order_confirmation(db, business, customer, session)

    # Nothing missing — create order
    return await _handle_order_confirmation(db, business, customer, session)


def _check_missing_details(business: Business, session) -> str | None:
    """Check what details are still missing and return a prompt, or None."""
    ctx = session.context_json or {}
    order_mode = ctx.get("order_mode", "PICKUP")
    already = {}
    if ctx.get("customer_name"):
        already["customer_name"] = True
    if ctx.get("phone_number"):
        already["phone_number"] = True
    if ctx.get("delivery_address"):
        already["delivery_address"] = True

    prompt = responses.collecting_details_response(
        business.require_customer_name,
        business.require_phone_number,
        business.require_delivery_address and order_mode == "DELIVERY",
        already,
    )
    return prompt if prompt else None


# ── Helper functions ─────────────────────────────────────────────────────────

async def _get_or_create_customer(
    db: AsyncSession,
    business_id: uuid.UUID,
    wa_id: str,
    display_name: str | None,
) -> Customer:
    """Get or create a customer by wa_id for this business."""
    result = await db.execute(
        select(Customer).where(
            Customer.business_id == business_id,
            Customer.wa_id == wa_id,
        )
    )
    customer = result.scalar_one_or_none()

    if customer:
        customer.last_message_at = datetime.now(timezone.utc)
        if display_name and not customer.display_name:
            customer.display_name = display_name
        return customer

    customer = Customer(
        business_id=business_id,
        wa_id=wa_id,
        display_name=display_name,
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(customer)
    await db.flush()
    return customer


def _persist_inbound(
    db: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    wa_message_id: str,
    text: str,
    raw_payload: dict,
    intent: str | None,
) -> Message:
    """Persist an inbound message to the messages table."""
    msg = Message(
        business_id=business_id,
        customer_id=customer_id,
        wa_message_id=wa_message_id,
        direction="INBOUND",
        text=text,
        payload_json=raw_payload,
        intent=intent,
    )
    db.add(msg)
    return msg


async def _send_response(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    wa_id: str,
    text: str,
    is_llm: bool = False,
    llm_tokens: int | None = None,
    llm_cost_cents: int | None = None,
    llm_provider: str | None = None,
    intent: str | None = None,
) -> None:
    """Send a response via WhatsApp and persist it (Model 1: platform token from env)."""
    if not business.whatsapp_phone_number_id:
        logger.warning("Business %s missing phone_number_id, skipping send", business.id)
        # Still persist the outbound message for records
        outbound = Message(
            business_id=business.id,
            customer_id=customer.id,
            direction="OUTBOUND",
            text=text,
            is_llm=is_llm,
            llm_tokens=llm_tokens,
            llm_cost_cents=llm_cost_cents,
            llm_provider=llm_provider,
            intent=intent,
        )
        db.add(outbound)
        return

    if not getattr(business, "is_whatsapp_enabled", True):
        logger.info("Business %s has WhatsApp disabled, skipping send", business.id)
        return

    await whatsapp_sender.send_text_message(
        db=db,
        business_id=business.id,
        customer_wa_id=wa_id,
        phone_number_id=business.whatsapp_phone_number_id,
        text=text,
        is_llm=is_llm,
        llm_tokens=llm_tokens,
        llm_cost_cents=llm_cost_cents,
        llm_provider=llm_provider,
        intent=intent,
        customer_id=customer.id,
    )


async def _load_menu(
    db: AsyncSession, business_id: uuid.UUID
) -> tuple[list[MenuCategory], list[MenuItem]]:
    """Load active menu categories and items for a business."""
    cats_result = await db.execute(
        select(MenuCategory).where(
            MenuCategory.business_id == business_id,
            MenuCategory.is_active == True,
        ).order_by(MenuCategory.sort_order)
    )
    items_result = await db.execute(
        select(MenuItem).where(
            MenuItem.business_id == business_id,
            MenuItem.is_active == True,
            MenuItem.is_deleted == False,
        ).order_by(MenuItem.sort_order)
    )
    return list(cats_result.scalars().all()), list(items_result.scalars().all())


async def _load_specials(db: AsyncSession, business_id: uuid.UUID) -> list[Special]:
    """Load active specials for a business."""
    result = await db.execute(
        select(Special).where(
            Special.business_id == business_id,
            Special.is_active == True,
        ).order_by(Special.sort_order)
    )
    return list(result.scalars().all())


async def _get_last_order(db: AsyncSession, business_id: uuid.UUID, customer_id: uuid.UUID):
    """Get the most recent order for a customer."""
    from shared.models.order import Order
    result = await db.execute(
        select(Order).where(
            Order.business_id == business_id,
            Order.customer_id == customer_id,
        ).order_by(Order.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()
