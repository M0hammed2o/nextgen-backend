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
9. Log business-hours context (no hard closed gate)
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
    Always commits at the end (or rolls back on error).
    """
    try:
        await _process(
            db=db,
            phone_number_id=phone_number_id,
            wa_message_id=wa_message_id,
            wa_id=wa_id,
            msg_text=msg_text,
            msg_type=msg_type,
            raw_payload=raw_payload,
            contact_name=contact_name,
        )
        await db.commit()
        logger.info("PIPELINE_COMMITTED: wa_message_id=%s", wa_message_id)
    except Exception:
        logger.exception(
            "PIPELINE_EXCEPTION: wa_message_id=%s, phone_number_id=%s — rolling back",
            wa_message_id,
            phone_number_id,
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
    logger.warning(
        "PIPELINE_START: wa_message_id=%s, phone_number_id=%s, wa_id=%s, type=%s",
        wa_message_id,
        phone_number_id,
        wa_id,
        msg_type,
    )

    # ── 1. Resolve business ──────────────────────────────────────────────
    result = await db.execute(
        select(Business).where(Business.whatsapp_phone_number_id == phone_number_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        logger.warning(
            "PIPELINE_NO_BUSINESS: phone_number_id=%s — no business registered",
            phone_number_id,
        )
        return

    logger.info(
        "PIPELINE_BUSINESS_FOUND: business_id=%s, name=%s, active=%s, whatsapp_enabled=%s",
        business.id,
        business.name,
        business.is_active,
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
        customer.id,
        customer.opted_out,
        wa_id,
    )

    # ── 4. Idempotency ───────────────────────────────────────────────────
    existing = await db.execute(
        select(Message.id).where(Message.wa_message_id == wa_message_id)
    )
    if existing.scalar_one_or_none():
        logger.info(
            "PIPELINE_DUPLICATE: wa_message_id=%s already processed, skipping",
            wa_message_id,
        )
        return

    # ── 5. Check opt-out ─────────────────────────────────────────────────
    if customer.opted_out:
        logger.info(
            "PIPELINE_OPTED_OUT: customer_id=%s, wa_id=%s, skipping",
            customer.id,
            wa_id,
        )
        return

    # ── 6. Detect opt-out intent early ───────────────────────────────────
    intent = intent_router.match_intent(msg_text)
    if intent == MessageIntent.OPT_OUT:
        customer.opted_out = True
        _persist_inbound(
            db=db,
            business_id=business.id,
            customer_id=customer.id,
            wa_message_id=wa_message_id,
            text=msg_text,
            raw_payload=raw_payload,
            intent="OPT_OUT",
        )
        await _send_response(
            db=db,
            business=business,
            customer=customer,
            wa_id=wa_id,
            text=responses.opted_out_response(),
            intent="OPT_OUT",
        )
        await usage_tracker.increment_usage(
            db,
            business.id,
            business.timezone,
            inbound_messages=1,
            outbound_messages=1,
        )
        return

    # ── 7. Persist inbound message ───────────────────────────────────────
    _persist_inbound(
        db=db,
        business_id=business.id,
        customer_id=customer.id,
        wa_message_id=wa_message_id,
        text=msg_text,
        raw_payload=raw_payload,
        intent=intent.value if intent else None,
    )

    # ── 8. Check daily message limit ─────────────────────────────────────
    try:
        await usage_tracker.check_daily_limit(db, business, "messages")
    except DailyLimitError:
        logger.warning("PIPELINE_LIMIT_HIT: business_id=%s message limit hit", business.id)
        return

    # ── 9. Load conversation session ─────────────────────────────────────
    session = await state_machine.get_or_create_session(db, business.id, customer.id)

    # ── 10. Business-hours context only (NO auto-closed gate) ───────────
    hours_configured = bool(business.business_hours)
    is_open_now = (
        is_business_open(business.business_hours, business.timezone)
        if hours_configured
        else True
    )

    logger.warning(
        "PIPELINE_HOURS_CHECK: business_id=%s, hours_configured=%s, is_open=%s, "
        "closed_text_present=%s, timezone=%s — no closed gate applied",
        business.id,
        hours_configured,
        is_open_now,
        bool(business.closed_text and str(business.closed_text).strip()),
        business.timezone,
    )

    # ── 11. Process based on intent + state ──────────────────────────────
    logger.warning(
        "PIPELINE_HANDLE_START: business_id=%s, wa_id=%s, intent=%s, session_state=%s, msg_text=%r",
        business.id,
        wa_id,
        intent.value if intent else "None",
        session.state,
        msg_text[:80],
    )

    response_text, is_llm, llm_tokens, llm_cost, llm_provider = await _handle_message(
        db=db,
        business=business,
        customer=customer,
        session=session,
        msg_text=msg_text,
        intent=intent,
    )

    # ── 12. Send response ────────────────────────────────────────────────
    if response_text:
        logger.warning(
            "PIPELINE_SENDING_REPLY: business_id=%s, wa_id=%s, intent=%s, "
            "is_llm=%s, text_len=%d, text_preview=%r",
            business.id,
            wa_id,
            intent.value if intent else "UNKNOWN",
            is_llm,
            len(response_text),
            response_text[:80],
        )
        await _send_response(
            db=db,
            business=business,
            customer=customer,
            wa_id=wa_id,
            text=response_text,
            is_llm=is_llm,
            llm_tokens=llm_tokens,
            llm_cost_cents=llm_cost,
            llm_provider=llm_provider,
            intent=intent.value if intent else "UNKNOWN",
        )
    else:
        logger.warning(
            "PIPELINE_NO_REPLY: business_id=%s, wa_id=%s, intent=%s",
            business.id,
            wa_id,
            intent.value if intent else "UNKNOWN",
        )

    # ── 13. Update usage ─────────────────────────────────────────────────
<<<<<<< HEAD
    await usage_tracker.increment_usage(
        db=db,
        business_id=business.id,
        timezone_str=business.timezone,
        inbound_messages=1,
        outbound_messages=1 if response_text else 0,
        llm_calls=1 if is_llm else 0,
        llm_tokens=llm_tokens or 0,
        llm_cost_cents=llm_cost or 0,
    )
=======
    # Wrapped in try/except: a usage-tracking failure must NEVER rollback
    # a message that was already sent to the customer.
    try:
        await usage_tracker.increment_usage(
            db=db,
            business_id=business.id,
            tz_name=business.timezone,
            inbound_messages=1,
            outbound_messages=1 if response_text else 0,
            llm_calls=1 if is_llm else 0,
            llm_tokens=llm_tokens or 0,
            llm_cost_cents=llm_cost or 0,
        )
    except Exception:
        logger.exception(
            "PIPELINE_USAGE_ERROR: increment_usage failed — message was sent, "
            "usage tracking skipped. business_id=%s",
            business.id,
        )
>>>>>>> 92bdbf8 (Fix Messages and Make it Work)


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
    Returns:
      (response_text, is_llm, llm_tokens, llm_cost_cents, llm_provider)
    """
    current_state = session.state
    logger.warning(
        "HANDLE_MSG: intent=%s, state=%s, msg=%r",
        intent.value if intent else "None",
        current_state,
        msg_text[:80],
    )

    # ── Template responses (no LLM needed) ───────────────────────────────

    if intent == MessageIntent.GREETING:
        logger.warning(
            "HANDLE_BRANCH: GREETING → greeting_response (greeting_text=%r)",
            business.greeting_text,
        )
        state_machine.transition_state(session, ConversationState.GREETING.value)
        greeting = responses.greeting_response(business)
        # Append today's specials preview if any are active
        specials = await _load_specials(db, business.id)
        todays_specials = responses.get_todays_active_specials(specials)
        if todays_specials:
            preview = "\n\n🔥 *Today's Specials:*\n" + "\n".join(
                f"• {s.title}" for s in todays_specials[:3]
            )
            preview += '\n\nSay *"specials"* for more details!'
            greeting = greeting + preview
        return greeting, False, None, None, None

    if intent == MessageIntent.MENU_REQUEST:
        menu_image_url = getattr(business, "menu_image_url", None)
        logger.warning(
            "HANDLE_BRANCH: MENU_REQUEST → menu_response (has_image=%s)",
            bool(menu_image_url),
        )
        categories, items = await _load_menu(db, business.id)
        state_machine.transition_state(session, ConversationState.BROWSING_MENU.value)

        if menu_image_url:
            # Send image first (non-blocking — failure is logged, not raised)
            try:
                await whatsapp_sender.send_image_message(
                    phone_number_id=business.whatsapp_phone_number_id,
                    recipient_wa_id=customer.wa_id,
                    image_url=menu_image_url,
                    caption="Here's our menu 👇",
                )
            except Exception:
                logger.exception(
                    "PIPELINE_IMAGE_SEND_FAIL: failed to send menu image, "
                    "falling back to text menu. business_id=%s",
                    business.id,
                )

        # Always return text menu (with or without image)
        text = responses.menu_response(categories, items, business.currency)
        if menu_image_url:
            text = "Let me know what you'd like to order! 😊\n\n" + text
        return text, False, None, None, None

    if intent == MessageIntent.SPECIALS_REQUEST:
        logger.warning("HANDLE_BRANCH: SPECIALS_REQUEST → specials_response")
        specials = await _load_specials(db, business.id)
        todays_specials = responses.get_todays_active_specials(specials)
        # Send images for specials that have them (before the text summary)
        for special in todays_specials:
            if special.image_url:
                try:
                    await whatsapp_sender.send_image_message(
                        phone_number_id=business.whatsapp_phone_number_id,
                        recipient_wa_id=customer.wa_id,
                        image_url=special.image_url,
                        caption=special.title,
                    )
                except Exception:
                    logger.exception(
                        "PIPELINE_SPECIALS_IMAGE_FAIL: failed to send image for special_id=%s",
                        special.id,
                    )
        return responses.specials_response(specials, business.currency), False, None, None, None

    if intent == MessageIntent.HOURS_REQUEST:
        logger.warning("HANDLE_BRANCH: HOURS_REQUEST → hours_response")
        return responses.hours_response(business), False, None, None, None

    if intent == MessageIntent.LOCATION_REQUEST:
        logger.warning("HANDLE_BRANCH: LOCATION_REQUEST → location_response")
        return responses.location_response(business), False, None, None, None

    if intent == MessageIntent.ORDER_CANCEL:
        logger.warning("HANDLE_BRANCH: ORDER_CANCEL → clearing cart")
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return (
            "Order cancelled. Your cart has been cleared. 🗑️\nAnything else I can help with?",
            False,
            None,
            None,
            None,
        )

    if intent == MessageIntent.ORDER_TRACK:
        logger.warning("HANDLE_BRANCH: ORDER_TRACK → checking last order")
        last_order = await _get_last_order(db, business.id, customer.id)
        if last_order:
            from shared.utils.money import format_currency

            return (
                f"📦 *Order {last_order.order_number}*\n"
                f"Status: *{last_order.status}*\n"
                f"Total: {format_currency(last_order.total_cents, business.currency)}\n"
                f"Placed: {last_order.created_at.strftime('%H:%M')}",
                False,
                None,
                None,
                None,
            )
        return "I couldn't find a recent order. Please check your order number.", False, None, None, None

    if intent == MessageIntent.VIEW_CART:
        logger.warning("HANDLE_BRANCH: VIEW_CART → cart summary")
        cart = state_machine.get_cart(session)
        if cart:
            summary = state_machine.cart_summary_text(session, business.currency)
            return (
                f"Got it! Here's what you have so far 🛒\n\n{summary}\n\n"
                'To confirm, say *"done"*. To add more, just tell me what you\'d like.',
                False, None, None, None,
            )
        return "Your cart is empty. Say *\"menu\"* to see what we have! 😊", False, None, None, None

    if intent == MessageIntent.HUMAN_HANDOFF:
        logger.warning("HANDLE_BRANCH: HUMAN_HANDOFF → transitioning to HANDOFF state")
        state_machine.transition_state(session, ConversationState.HANDOFF.value)
        return (
            "No problem! 👋 Let me connect you with our team.\n"
            "A staff member will assist you shortly. Please hang tight!",
            False, None, None, None,
        )

    # ── Order confirmation (in CONFIRMING_ORDER state) ───────────────────
    if current_state == ConversationState.CONFIRMING_ORDER.value:
        is_confirm = intent_router.is_confirmation(msg_text)
        is_negate = intent_router.is_negation(msg_text)
        logger.warning(
            "HANDLE_BRANCH: CONFIRMING_ORDER state — is_confirm=%s, is_negate=%s",
            is_confirm,
            is_negate,
        )
        if is_confirm:
            return await _handle_order_confirmation(db, business, customer, session)
        if is_negate:
            state_machine.transition_state(session, ConversationState.BUILDING_CART.value)
            return (
                'No problem! What would you like to change?\n• Add more items\n• Remove something\n• Cancel the order',
                False,
                None,
                None,
                None,
            )

    # ── Collecting details state ─────────────────────────────────────────
    if current_state == ConversationState.COLLECTING_DETAILS.value:
        logger.warning("HANDLE_BRANCH: COLLECTING_DETAILS state → collecting details")
        return await _handle_collecting_details(db, business, customer, session, msg_text)

    # ── LLM-required intents (ordering, ambiguous) ───────────────────────
    needs_llm_call = intent_router.needs_llm(intent, current_state)
    logger.warning(
        "HANDLE_BRANCH: needs_llm=%s, intent=%s, state=%s",
        needs_llm_call,
        intent.value if intent else "None",
        current_state,
    )

    if needs_llm_call:
        try:
            await usage_tracker.check_daily_limit(db, business, "llm_calls")
        except DailyLimitError:
            logger.warning("HANDLE_BRANCH: LLM daily limit hit → fallback_response")
            return responses.fallback_response(business), False, None, None, None

        logger.warning("HANDLE_BRANCH: → LLM call")
        return await _handle_with_llm(db, business, customer, session, msg_text)

    # ── Fallback ─────────────────────────────────────────────────────────
    logger.warning(
        "HANDLE_BRANCH: FALLBACK — intent=%s, state=%s, fallback_text=%r",
        intent.value if intent else "None",
        current_state,
        business.fallback_text,
    )
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

    system_prompt = prompt_builder.build_system_prompt(
        business,
        categories,
        items,
        specials,
        session.state,
        cart,
    )

    from backend.app.llm.provider import get_llm_provider

    provider = get_llm_provider()
    llm_response = await provider.complete(system_prompt, msg_text)

    parsed = llm_parser.parse_llm_response(llm_response.text)

    if parsed.action == "add_items" and parsed.items:
        items_map = {i.name.lower(): i for i in items if i.is_active and not i.is_deleted}
        added: list[str] = []
        unmatched: list[str] = []

        for pi in parsed.items:
            if not pi.name:
                if pi.original_text:
                    unmatched.append(pi.original_text)
                continue

            matched_item = items_map.get(pi.name.lower())
            if not matched_item:
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

        response_parts: list[str] = []
        if added:
            response_parts.append("Added to your order: " + ", ".join(added) + " ✅")
        if unmatched:
            response_parts.append(
                f"Sorry, I couldn't find: {', '.join(unmatched)}. Check our menu for available items."
            )

        response_parts.append("\n" + state_machine.cart_summary_text(session, business.currency))
        response_parts.append('\nAnything else? Or say *"done"* to confirm your order.')

        return (
            "\n".join(response_parts),
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    if parsed.action == "remove_item" and parsed.items:
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

        return (
            msg,
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    if parsed.action == "confirm_order":
        cart = state_machine.get_cart(session)
        if not cart:
            return (
                "Your cart is empty. What would you like to order?",
                True,
                llm_response.total_tokens,
                llm_response.cost_cents,
                llm_response.provider,
            )

        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        summary = state_machine.cart_summary_text(session, business.currency)
        total = state_machine.cart_total_cents(session)

        return (
            responses.ask_confirmation_response(
                summary,
                total,
                business.delivery_fee_cents
                if state_machine.get_context(session, "order_mode") == "DELIVERY"
                else 0,
                state_machine.get_context(session, "order_mode", "PICKUP"),
                business.currency,
            ),
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    if parsed.action == "cancel_order":
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return (
            "Order cancelled. 🗑️ Anything else?",
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    if parsed.action == "handoff":
        state_machine.transition_state(session, ConversationState.HANDOFF.value)
        return (
            "Let me connect you with our team. 👋\n"
            "A staff member will assist you shortly. Please hang tight!",
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    return (
        parsed.message,
        True,
        llm_response.total_tokens,
        llm_response.cost_cents,
        llm_response.provider,
    )


async def _handle_order_confirmation(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Handle order confirmation — check details, create order."""
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
        need_name,
        need_phone,
        need_address,
        already_have,
    )

    if details_prompt:
        state_machine.transition_state(session, ConversationState.COLLECTING_DETAILS.value)
        return details_prompt, False, None, None, None

    try:
        await usage_tracker.check_daily_limit(db, business, "orders")
    except DailyLimitError:
        return (
            "Sorry, we can't accept more orders right now. Please try again later.",
            False,
            None,
            None,
            None,
        )

    order = await order_creator.create_order_from_cart(db, business, customer, session)

    summary = state_machine.cart_summary_text(session, business.currency)
    response_text = responses.order_confirmation_response(
        order.order_number,
        summary,
        order.subtotal_cents,
        order.delivery_fee_cents,
        order.order_mode,
        business.currency,
    )

    await usage_tracker.increment_usage(
        db,
        business.id,
        business.timezone,
        orders_created=1,
        revenue_cents=order.total_cents,
    )

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

    if business.require_customer_name and "customer_name" not in ctx:
        state_machine.set_context(session, "customer_name", text)
        customer.display_name = text
        still_missing = _check_missing_details(business, session)
        if still_missing:
            return still_missing, False, None, None, None
        return await _handle_order_confirmation(db, business, customer, session)

    if business.require_phone_number and "phone_number" not in ctx:
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


# ── Helper functions ─────────────────────────────────────────────────────

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
    """Send a response via WhatsApp and persist it."""
    if not business.whatsapp_phone_number_id:
        logger.warning(
            "SEND_SKIP: business %s missing phone_number_id, persisting outbound only",
            business.id,
        )
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
        logger.info("SEND_SKIP: business %s has WhatsApp disabled", business.id)
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
    db: AsyncSession,
    business_id: uuid.UUID,
) -> tuple[list[MenuCategory], list[MenuItem]]:
    """Load active menu categories and items for a business."""
    cats_result = await db.execute(
        select(MenuCategory)
        .where(
            MenuCategory.business_id == business_id,
            MenuCategory.is_active.is_(True),
        )
        .order_by(MenuCategory.sort_order)
    )
    items_result = await db.execute(
        select(MenuItem)
        .where(
            MenuItem.business_id == business_id,
            MenuItem.is_active.is_(True),
            MenuItem.is_deleted.is_(False),
        )
        .order_by(MenuItem.sort_order)
    )
    return list(cats_result.scalars().all()), list(items_result.scalars().all())


async def _load_specials(db: AsyncSession, business_id: uuid.UUID) -> list[Special]:
    """Load active specials for a business."""
    result = await db.execute(
        select(Special)
        .where(
            Special.business_id == business_id,
            Special.is_active.is_(True),
        )
        .order_by(Special.sort_order)
    )
    return list(result.scalars().all())


async def _get_last_order(
    db: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
):
    """Get the most recent order for a customer."""
    from shared.models.order import Order

    result = await db.execute(
        select(Order)
        .where(
            Order.business_id == business_id,
            Order.customer_id == customer_id,
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()