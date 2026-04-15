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

from sqlalchemy import select, update as _sa_update
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
from shared.models.order import Order as _Order, OrderEvent as _OrderEvent
from shared.models.specials import Special
from shared.utils.time import is_business_open

logger = logging.getLogger("nextgen.bot.pipeline")


def _parse_uuid(val) -> uuid.UUID | None:
    """Safely parse a UUID string; return None on failure."""
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError):
        return None


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
            "HANDLE_BRANCH: MENU_REQUEST → menu_response (has_image=%s, state=%s)",
            bool(menu_image_url),
            current_state,
        )
        categories, items = await _load_menu(db, business.id)

        # Don't transition away from BUILDING_CART or CONFIRMING_ORDER —
        # customer is browsing mid-order. Keep their order context intact.
        _order_active_states = {
            ConversationState.BUILDING_CART.value,
            ConversationState.CONFIRMING_ORDER.value,
            ConversationState.COLLECTING_DETAILS.value,
        }
        if current_state not in _order_active_states:
            state_machine.transition_state(session, ConversationState.BROWSING_MENU.value)

        # Only send image if URL is a direct image file (not a page/album URL)
        _direct_image = bool(
            menu_image_url
            and any(
                menu_image_url.lower().split("?")[0].endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
            )
        )
        if menu_image_url and _direct_image:
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
        elif menu_image_url:
            logger.warning(
                "PIPELINE_IMAGE_SKIP: not a direct image URL, including as link. url=%s",
                menu_image_url,
            )

        text = responses.menu_response(categories, items, business.currency)
        cart = state_machine.get_cart(session)
        if current_state in _order_active_states and cart:
            text = (
                "Here's the full menu! Your current order is still saved 🛒\n\n"
                + text
                + '\n\nJust tell me what you\'d like to add, or say *"done"* to confirm your order.'
            )
        else:
            header = "Here's our menu 😊"
            if menu_image_url and not _direct_image:
                header += f"\n📷 View menu image: {menu_image_url}"
            text = header + "\n\n" + text + "\n\nLet me know what you'd like to order! 😊"
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
        # Don't intercept when collecting details — "Address is 22 Kings Ave"
        # means the customer is providing their delivery address, not asking
        # for ours. Let COLLECTING_DETAILS handle it normally.
        if current_state != ConversationState.COLLECTING_DETAILS.value:
            logger.warning("HANDLE_BRANCH: LOCATION_REQUEST → location_response")
            return responses.location_response(business), False, None, None, None
        logger.warning(
            "HANDLE_BRANCH: LOCATION_REQUEST skipped — state=COLLECTING_DETAILS, "
            "treating as detail input. session_id=%s", session.id
        )

    if intent == MessageIntent.ORDER_CANCEL:
        logger.warning("HANDLE_BRANCH: ORDER_CANCEL → cancelling live orders + clearing cart")
        # Cancel any live DB orders for this customer so the staff dashboard
        # reflects the cancellation immediately. Fire-and-forget — never raises.
        await _cancel_prior_live_orders(db, business, customer, exclude_order_id=None)
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return (
            "Order cancelled. Your cart has been cleared. 🗑️\nAnything else I can help with?",
            False,
            None,
            None,
            None,
        )

    # ── ORDER_PLACED guard: block add/modify if order already in progress ────
    # Statuses that still allow modification: PENDING_DELIVERY_FEE, FEE_SENT, NEW, ACCEPTED
    _MODIFIABLE_STATUSES = {"PENDING_DELIVERY_FEE", "FEE_SENT", "NEW", "ACCEPTED"}
    _IN_PROGRESS_STATUSES = {"IN_PROGRESS", "READY"}  # COLLECTED/DELIVERED are terminal — new orders allowed
    if (
        current_state == ConversationState.ORDER_PLACED.value
        and intent in (MessageIntent.ORDER_START, MessageIntent.ORDER_ADD, MessageIntent.ORDER_REMOVE)
    ):
        last_order = await _get_last_order(db, business.id, customer.id)
        if last_order and last_order.status in _IN_PROGRESS_STATUSES:
            logger.warning(
                "ORDER_PLACED_GUARD: order=%s status=%s, blocking modification",
                last_order.order_number, last_order.status,
            )
            return (
                "Your order is already being prepared. Please contact the store if you need to make changes. 🙏",
                False, None, None, None,
            )
        # Order is still modifiable — save its ID so the next confirmed order
        # can cancel it, preventing it from lingering in the live queue.
        if last_order and last_order.status in _MODIFIABLE_STATUSES:
            state_machine.set_context(session, "superseded_order_id", str(last_order.id))
            logger.warning(
                "ORDER_PLACED_GUARD: order=%s status=%s, allowing modification — superseded_order_id saved",
                last_order.order_number, last_order.status,
            )
        state_machine.transition_state(session, ConversationState.BUILDING_CART.value)
        # Return a clear framing message so the customer knows they're starting a
        # fresh replacement order. Without this the bot falls through with an empty
        # cart and misprocesses the original ORDER_REMOVE / ORDER_ADD intent.
        order_ref = f" (replacing {last_order.order_number})" if last_order else ""
        return (
            f"Got it{order_ref}! I've started a new order for you. "
            f"Please tell me what you'd like — just send your full order again and I'll build it from scratch. 🛒",
            False, None, None, None,
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

    # Only intercept ORDER_CONFIRM when NOT already in a state that manages
    # confirmation itself. Falls through in CONFIRMING_ORDER (so "yes" places
    # the order), WAITING_DELIVERY_FEE_APPROVAL (so "yes" approves the fee),
    # and COLLECTING_PAYMENT (so "cash"/"card" is handled by that state).
    _SKIP_CONFIRM_GATE_STATES = {
        ConversationState.CONFIRMING_ORDER.value,
        ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value,
        ConversationState.COLLECTING_PAYMENT.value,
    }
    if intent == MessageIntent.ORDER_CONFIRM and current_state not in _SKIP_CONFIRM_GATE_STATES:
        logger.warning("HANDLE_BRANCH: ORDER_CONFIRM → cart confirmation gate (state=%s)", current_state)
        cart = state_machine.get_cart(session)
        if not cart:
            return (
                "Your cart is empty. 🛒\nSay *\"menu\"* to see what we have!",
                False, None, None, None,
            )
        # Lock cart snapshot — order creation always reads confirmed_cart, never live cart
        import copy
        locked = copy.deepcopy(cart)
        state_machine.set_context(session, "confirmed_cart", locked)
        logger.warning(
            "CART_LOCKED: session_id=%s, items=%d, total_cents=%d",
            session.id, len(locked), sum(i["line_total_cents"] for i in locked),
        )
        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        summary = state_machine.cart_summary_text(session, business.currency)
        total = state_machine.cart_total_cents(session)
        order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
        confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
        if order_mode == "DELIVERY":
            confirm_msg += "\n_Delivery fee will be confirmed by our team._"
        return (confirm_msg, False, None, None, None)

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
            # If the confirmation message mentions delivery/pickup, apply it now
            # before entering order confirmation (e.g. "yes for delivery").
            import re as _re2
            if _re2.search(r"\b(delivery|deliver)\b", msg_text, _re2.I):
                state_machine.set_context(session, "order_mode", "DELIVERY")
                logger.warning("CONFIRM_MODE_SET: DELIVERY from confirmation msg. session_id=%s", session.id)
            elif _re2.search(r"\b(pickup|pick\s+up|collection|collect)\b", msg_text, _re2.I):
                state_machine.set_context(session, "order_mode", "PICKUP")
                logger.warning("CONFIRM_MODE_SET: PICKUP from confirmation msg. session_id=%s", session.id)
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
        # Cart correction: customer is restating the full order with corrected quantities.
        # Clear the cart, transition to BUILDING_CART, then call LLM with ORDER_START so that
        # real add_to_cart() logic executes and confirmed_cart is correctly locked afterward.
        if intent_router.is_cart_correction(msg_text):
            state_machine.clear_cart(session)
            state_machine.transition_state(session, ConversationState.BUILDING_CART.value)
            logger.warning(
                "CART_CORRECTION: cleared cart, transitioning to BUILDING_CART, rebuilding via LLM. session_id=%s, msg=%r",
                session.id, msg_text[:80],
            )
            return await _handle_with_llm(db, business, customer, session, msg_text, intent=MessageIntent.ORDER_START)

    # ── Choosing options state (e.g. "what size?") ──────────────────────
    if current_state == ConversationState.CHOOSING_OPTIONS.value:
        logger.warning("HANDLE_BRANCH: CHOOSING_OPTIONS → LLM resolves pending item options")
        return await _handle_with_llm(db, business, customer, session, msg_text, intent=intent)

    # ── Choosing order mode (pickup vs delivery) ─────────────────────────
    if current_state == ConversationState.CHOOSING_ORDER_MODE.value:
        logger.warning("HANDLE_BRANCH: CHOOSING_ORDER_MODE → parsing pickup/delivery choice")
        return await _handle_choosing_order_mode(db, business, customer, session, msg_text)

    # ── Waiting for delivery fee approval from customer ──────────────────
    if current_state == ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value:
        logger.warning("HANDLE_BRANCH: WAITING_DELIVERY_FEE_APPROVAL → checking fee + answer")
        return await _handle_waiting_delivery_fee(db, business, customer, session, msg_text)

    # ── Collecting payment method (cash / card) ───────────────────────────
    if current_state == ConversationState.COLLECTING_PAYMENT.value:
        logger.warning("HANDLE_BRANCH: COLLECTING_PAYMENT → parsing payment choice")
        return await _handle_collecting_payment(db, business, customer, session, msg_text)

    # ── Collecting details state ─────────────────────────────────────────
    if current_state == ConversationState.COLLECTING_DETAILS.value:
        logger.warning("HANDLE_BRANCH: COLLECTING_DETAILS state → collecting details")
        return await _handle_collecting_details(db, business, customer, session, msg_text)

    # ── Recommendation acceptance ─────────────────────────────────────────
    # Must come BEFORE the LLM gate: when the customer says "I'll take those"
    # after a recommendation, we convert stored items to real cart entries
    # without an LLM call (deterministic, no hallucination possible).
    _recommended = state_machine.get_context(session, "recommended_items")
    if _recommended and intent_router.is_recommendation_acceptance(msg_text):
        logger.warning(
            "REC_ACCEPT: acceptance detected, %d stored item(s), session_id=%s, msg=%r",
            len(_recommended), session.id, msg_text[:60],
        )
        return await _handle_recommendation_acceptance(db, business, customer, session, _recommended)

    # Customer did NOT accept the recommendation (gate above returned early if they had).
    # Clear it now so the LLM doesn't re-add recommended items alongside new ones.
    if _recommended:
        state_machine.set_context(session, "recommended_items", None)
        logger.warning(
            "REC_CLEARED: customer moved on, cleared recommended_items. session_id=%s",
            session.id,
        )

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
        return await _handle_with_llm(db, business, customer, session, msg_text, intent=intent)

    # ── Fallback ─────────────────────────────────────────────────────────
    logger.warning(
        "HANDLE_BRANCH: FALLBACK — intent=%s, state=%s, fallback_text=%r",
        intent.value if intent else "None",
        current_state,
        business.fallback_text,
    )
    return responses.fallback_response(business), False, None, None, None


# ── Size-variant helpers ─────────────────────────────────────────────────────
# Words that indicate a size choice inside an item name.
_SIZE_KEYWORDS: frozenset[str] = frozenset({
    "small", "medium", "large", "regular", "xl", "xxl", "mini",
    "sm", "md", "lg", "half", "full", "single", "double",
})

# Customer shorthand → substring that must appear in a menu item name.
# All keys and values lowercase. Add new entries here for each deployment.
_ITEM_ALIASES: dict[str, str] = {
    "coke":  "coca-cola",
    "cola":  "coca-cola",
    "wings": "wings",   # matches "Wings (6 pcs)", "Wings (12 pcs)", etc.
}


def _find_size_variants(msg_text: str, menu_items: list) -> tuple[str | None, list]:
    """
    Pre-LLM check: detect if the message refers to a menu item that has
    multiple size variants (e.g. Small Pizza / Medium Pizza / Large Pizza).

    Groups active items by their 'base name' (item name with size keywords stripped).
    Returns (base_name, [MenuItem, ...]) when:
      - The base name matches the message text, AND
      - At least 2 variants exist, AND
      - The message does NOT already contain a size keyword (so we only ask once).
    Otherwise returns (None, []).
    """
    active = [i for i in menu_items if i.is_active and not i.is_deleted]
    msg_lower = msg_text.lower()
    msg_words = set(msg_lower.split())

    # If the customer already stated a size, skip — no need to ask
    if msg_words & _SIZE_KEYWORDS:
        return None, []

    groups: dict[str, list] = {}
    for item in active:
        name_parts = item.name.lower().split()
        base_parts = [w for w in name_parts if w not in _SIZE_KEYWORDS]
        if not base_parts:
            continue
        base = " ".join(base_parts)
        groups.setdefault(base, []).append(item)

    for base, variants in groups.items():
        if len(variants) < 2:
            continue
        significant = [w for w in base.split() if len(w) > 3]
        if not significant:
            continue
        # Require ≥80% of the significant base words to appear in the message
        match_ratio = sum(1 for w in significant if w in msg_words) / len(significant)
        if match_ratio >= 0.8:
            return base, variants

    return None, []


def _parse_quantity_before(msg_lower: str, item_start: int) -> int:
    """Return the quantity token immediately before the item match, defaulting to 1."""
    prefix = msg_lower[:item_start].rstrip()
    if not prefix:
        return 1
    tokens = prefix.split()
    if not tokens:
        return 1
    last = tokens[-1].rstrip(".,")
    if last.isdigit():
        return max(1, min(int(last), 20))
    _WORD_NUMS = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }
    if last in _WORD_NUMS:
        return _WORD_NUMS[last]
    return 1


def _extract_items_from_chunk(
    chunk: str,
    active_sorted: list,
    global_consumed_names: set[str],
) -> list[tuple]:
    """
    Run alias + substring extraction against a single message chunk.
    `global_consumed_names` prevents the same menu item being returned twice
    across all chunks (e.g., "burger and burger" → 1 match, not 2 — quantity
    accumulation is handled by add_to_cart).
    """
    import re as _re
    chunk_lower = chunk.strip().lower()
    if not chunk_lower:
        return []

    matched: list[tuple] = []
    consumed_positions: set[int] = set()

    # ── Alias pass ────────────────────────────────────────────────────────
    for alias, target_substr in _ITEM_ALIASES.items():
        for m in _re.finditer(rf"\b{_re.escape(alias)}\b", chunk_lower):
            a_start, a_end = m.start(), m.end()
            span = set(range(a_start, a_end))
            if span & consumed_positions:
                continue
            candidates = [
                item for item in active_sorted
                if target_substr in item.name.lower()
                and item.name.lower() not in global_consumed_names
            ]
            if not candidates:
                continue
            best = candidates[0]
            qty = _parse_quantity_before(chunk_lower, a_start)
            matched.append((best, qty))
            consumed_positions |= span
            global_consumed_names.add(best.name.lower())
            break

    # ── Normal substring pass ─────────────────────────────────────────────
    for item in active_sorted:
        name_lower = item.name.lower()
        if len(name_lower) < 2:
            continue
        if name_lower in global_consumed_names:
            continue
        start = 0
        while True:
            idx = chunk_lower.find(name_lower, start)
            if idx == -1:
                break
            end = idx + len(name_lower)
            span = set(range(idx, end))
            if span & consumed_positions:
                start = idx + 1
                continue
            before_ok = idx == 0 or not chunk_lower[idx - 1].isalpha()
            after_ok = end == len(chunk_lower) or not chunk_lower[end].isalpha()
            if not (before_ok and after_ok):
                start = idx + 1
                continue
            qty = _parse_quantity_before(chunk_lower, idx)
            matched.append((item, qty))
            consumed_positions |= span
            global_consumed_names.add(name_lower)
            break

    return matched


def _extract_items_from_message(
    msg_text: str,
    menu_items: list,
) -> list[tuple]:
    """
    Deterministic pre-LLM item extraction.

    Splits the message on "and", "also", and "," then runs alias + substring
    extraction per chunk. Longest menu names tested first so "Cheesy Chips"
    wins over "Chips". Returns [] if nothing matched — caller falls back to LLM.
    """
    import re as _re
    active = [i for i in menu_items if i.is_active and not i.is_deleted]
    if not active:
        return []

    # Longest names first
    active_sorted = sorted(active, key=lambda i: len(i.name), reverse=True)

    # Split on natural item separators
    chunks = _re.split(r"\band\b|\balso\b|,", msg_text, flags=_re.I)

    all_matched: list[tuple] = []
    consumed_names: set[str] = set()
    for chunk in chunks:
        all_matched.extend(_extract_items_from_chunk(chunk, active_sorted, consumed_names))

    return all_matched


async def _handle_with_llm(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
    intent: MessageIntent | None = None,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Call LLM with conversation history, parse response, update cart/state."""
    categories, items = await _load_menu(db, business.id)
    specials = await _load_specials(db, business.id)
    cart = state_machine.get_cart(session)

    # ── Deterministic size-ambiguity check (no LLM for the question) ────────
    # Skip when already in CHOOSING_OPTIONS — customer is answering the question.
    # Skip when the message already contains a size word — no ambiguity exists.
    import re as _re
    _SIZE_WORDS = _re.compile(r"\b(small|medium|large)\b", _re.I)
    if (
        session.state != ConversationState.CHOOSING_OPTIONS.value
        and not _SIZE_WORDS.search(msg_text)
    ):
        base_name, variants = _find_size_variants(msg_text, items)
        if base_name and variants:
            pending = [{"name": base_name, "quantity": 1, "options": None, "special_instructions": None}]
            state_machine.set_context(session, "pending_options", pending)
            state_machine.transition_state(session, ConversationState.CHOOSING_OPTIONS.value)
            size_options = "\n".join(f"  • {v.name}" for v in variants)
            logger.warning(
                "SIZE_AMBIGUITY: base=%r, variants=%d, session_id=%s",
                base_name, len(variants), session.id,
            )
            return (
                f"Which *{base_name.title()}* would you like?\n{size_options}",
                False, None, None, None,
            )

    # ── Deterministic item extraction (pre-LLM, ORDER_START / ORDER_ADD) ───────
    # For ORDER_START: runs in any non-CHOOSING_OPTIONS state so that "Can I get
    # one Double Smash Burger" is always resolved deterministically regardless of
    # current state (IDLE, BROWSING_MENU, BUILDING_CART, CONFIRMING_ORDER).
    # For ORDER_ADD: BUILDING_CART only (add-ons in CONFIRMING_ORDER are handled
    # by the separate DET_ADD_CONFIRMING block below which checks for mutation signals).
    # CHOOSING_OPTIONS is always excluded — customer is answering a size/option question.
    _det_states_for_add = {ConversationState.BUILDING_CART.value}
    _det_eligible = (
        intent == MessageIntent.ORDER_START
        and session.state != ConversationState.CHOOSING_OPTIONS.value
    ) or (
        intent == MessageIntent.ORDER_ADD
        and session.state in _det_states_for_add
    )
    if _det_eligible:
        det_matches = _extract_items_from_message(msg_text, items)
        if det_matches:
            det_added: list[str] = []
            for det_item, det_qty in det_matches:
                state_machine.add_to_cart(
                    session,
                    menu_item_id=str(det_item.id),
                    name=det_item.name,
                    price_cents=det_item.price_cents,
                    quantity=det_qty,
                    options=None,
                    special_instructions=None,
                )
                det_added.append(f"{det_qty}x {det_item.name}")
            logger.warning(
                "DET_MATCH: added %d item(s) deterministically: %r, session_id=%s",
                len(det_added), det_added, session.id,
            )
            import copy as _copy
            updated_cart = state_machine.get_cart(session)
            state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(updated_cart))
            state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
            logger.warning(
                "CART_LOCKED_DET: items=%d, total_cents=%d, session_id=%s",
                len(updated_cart), sum(i["line_total_cents"] for i in updated_cart), session.id,
            )
            order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
            total = state_machine.cart_total_cents(session)
            summary = state_machine.cart_summary_text(session, business.currency)
            confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
            if order_mode == "DELIVERY":
                confirm_msg += "\n_Delivery fee will be confirmed by our team._"
            parts = ["Added to your order: " + ", ".join(det_added) + " ✅"]
            parts.append("\n" + confirm_msg)
            return ("\n".join(parts), False, None, None, None)

    # ── Deterministic add-on in CONFIRMING_ORDER (simple add-only messages) ──
    # Handles "Add the ice coffee", "Add wings" while reviewing cart.
    # Only fires when the message has no remove/replace/special-instruction signals.
    # Complex mutations (remove, swap, without) still go to LLM as before.
    import re as _re
    _CONFIRMING_MUTATION_SIGNALS = _re.compile(
        r"\b(remove|take\s+off|delete|instead\s+of|replace|swap|without)\b", _re.I
    )
    if (
        intent in (MessageIntent.ORDER_START, MessageIntent.ORDER_ADD)
        and session.state == ConversationState.CONFIRMING_ORDER.value
        and not _CONFIRMING_MUTATION_SIGNALS.search(msg_text)
    ):
        det_matches = _extract_items_from_message(msg_text, items)
        if det_matches:
            det_added: list[str] = []
            for det_item, det_qty in det_matches:
                state_machine.add_to_cart(
                    session,
                    menu_item_id=str(det_item.id),
                    name=det_item.name,
                    price_cents=det_item.price_cents,
                    quantity=det_qty,
                    options=None,
                    special_instructions=None,
                )
                det_added.append(f"{det_qty}x {det_item.name}")
            import copy as _copy
            updated_cart = state_machine.get_cart(session)
            state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(updated_cart))
            logger.warning(
                "DET_ADD_CONFIRMING: added %d item(s): %r, session_id=%s",
                len(det_added), det_added, session.id,
            )
            order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
            total = state_machine.cart_total_cents(session)
            summary = state_machine.cart_summary_text(session, business.currency)
            confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
            if order_mode == "DELIVERY":
                confirm_msg += "\n_Delivery fee will be confirmed by our team._"
            parts = ["Added: " + ", ".join(det_added) + " ✅"]
            parts.append("\n" + confirm_msg)
            return ("\n".join(parts), False, None, None, None)

    pending_options = state_machine.get_context(session, "pending_options")
    # recommended_items is cleared in _handle_message as soon as the customer
    # does anything other than accept the recommendation, so this will be None
    # in all normal ordering flows.
    recommended_items = state_machine.get_context(session, "recommended_items")

    system_prompt = prompt_builder.build_system_prompt(
        business,
        categories,
        items,
        specials,
        session.state,
        cart,
        pending_options=pending_options,
        recommended_items=recommended_items,
    )

    from backend.app.llm.provider import get_llm_provider

    provider = get_llm_provider()

    # Load conversation history so LLM has context across multiple messages
    history = await _load_conversation_history(db, business.id, customer.id, limit=8)
    # The current inbound message is already flushed to DB (step 7+9 flush).
    # If it's the last entry in history, use history as-is; otherwise append it.
    if not history or not (history[-1]["role"] == "user" and history[-1]["content"] == msg_text):
        history.append({"role": "user", "content": msg_text})

    llm_response = await provider.complete_with_history(system_prompt, history)

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
                # Fuzzy: longest menu name that is a substring of the LLM name
                llm_name_lower = pi.name.lower()
                candidates = [
                    (len(mn), mi) for mn, mi in items_map.items()
                    if mn in llm_name_lower and len(mn) / max(len(llm_name_lower), 1) >= 0.5
                ]
                if candidates:
                    matched_item = sorted(candidates, reverse=True)[0][1]

            if matched_item:
                safe_qty = max(1, min(int(pi.quantity or 1), 20))
                state_machine.add_to_cart(
                    session,
                    menu_item_id=str(matched_item.id),
                    name=matched_item.name,
                    price_cents=matched_item.price_cents,
                    quantity=safe_qty,
                    options=pi.options if pi.options else None,
                    special_instructions=pi.special_instructions,
                )
                added.append(f"{safe_qty}x {matched_item.name}")
            else:
                unmatched.append(pi.name)

        response_parts: list[str] = []
        if added:
            response_parts.append("Added to your order: " + ", ".join(added) + " ✅")
        if unmatched:
            response_parts.append(
                f"Sorry, I couldn't find: {', '.join(unmatched)}. Check our menu for available items."
            )

        # If items were added while resolving a pending options question, clear it.
        if session.state == ConversationState.CHOOSING_OPTIONS.value:
            state_machine.set_context(session, "pending_options", None)
            logger.warning("PENDING_OPTIONS_RESOLVED: cleared pending_options. session_id=%s", session.id)

        # Lock cart + go to CONFIRMING_ORDER so one "yes" places the order.
        # If already in CONFIRMING_ORDER, relock with updated cart.
        import copy as _copy
        updated_cart = state_machine.get_cart(session)
        state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(updated_cart))
        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        logger.warning(
            "CART_LOCKED_ON_ADD: items=%d, total_cents=%d",
            len(updated_cart), sum(i["line_total_cents"] for i in updated_cart),
        )
        order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
        total = state_machine.cart_total_cents(session)
        summary = state_machine.cart_summary_text(session, business.currency)
        confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
        if order_mode == "DELIVERY":
            confirm_msg += "\n_Delivery fee will be confirmed by our team._"
        response_parts.append("\n" + confirm_msg)

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
            # Always resync confirmed_cart to the current cart after any removal,
            # regardless of prior state — ensures the order snapshot is never stale.
            import copy as _copy
            state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(cart))
            state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
            order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
            total = state_machine.cart_total_cents(session)
            summary = state_machine.cart_summary_text(session, business.currency)
            confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
            if order_mode == "DELIVERY":
                confirm_msg += "\n_Delivery fee will be confirmed by our team._"
            msg = "Item removed. ✅\n" + confirm_msg
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

    if parsed.action == "replace_item" and parsed.items:
        items_map = {i.name.lower(): i for i in items if i.is_active and not i.is_deleted}
        replaced: list[str] = []
        replace_errors: list[str] = []

        for pi in parsed.items:
            remove_name = pi.remove or pi.name
            add_name = pi.add
            if not remove_name or not add_name:
                replace_errors.append("couldn't parse replacement")
                continue
            _, was_removed = state_machine.remove_from_cart(session, remove_name)
            new_item = items_map.get(add_name.lower())
            if not new_item:
                candidates = [
                    (len(mn), mi) for mn, mi in items_map.items()
                    if mn in add_name.lower() and len(mn) / max(len(add_name), 1) >= 0.5
                ]
                if candidates:
                    new_item = sorted(candidates, reverse=True)[0][1]
            if new_item:
                safe_qty = max(1, min(int(pi.quantity or 1), 20))
                state_machine.add_to_cart(
                    session, str(new_item.id), new_item.name,
                    new_item.price_cents, safe_qty,
                    options=pi.options if pi.options else None,
                    special_instructions=pi.special_instructions,
                )
                replaced.append(f"{remove_name} → {new_item.name}")
            else:
                if was_removed and remove_name:
                    orig = items_map.get(remove_name.lower())
                    if orig:
                        state_machine.add_to_cart(session, str(orig.id), orig.name, orig.price_cents, 1)
                replace_errors.append(f"couldn't find '{add_name}' on the menu")

        cart = state_machine.get_cart(session)
        parts = []
        if replaced:
            parts.append("Updated: " + ", ".join(replaced) + " ✅")
        if replace_errors:
            parts.append("Sorry: " + "; ".join(replace_errors) + ". Please check the menu.")
        if cart:
            import copy as _copy
            state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(cart))
            state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
            order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
            total = state_machine.cart_total_cents(session)
            summary = state_machine.cart_summary_text(session, business.currency)
            confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
            if order_mode == "DELIVERY":
                confirm_msg += "\n_Delivery fee will be confirmed by our team._"
            parts.append("\n" + confirm_msg)
        else:
            state_machine.transition_state(session, ConversationState.IDLE.value)
            parts.append("Your cart is now empty. What would you like to order?")

        return ("\n".join(parts), True, llm_response.total_tokens, llm_response.cost_cents, llm_response.provider)

    if parsed.action == "recommend_items" and parsed.items:
        # Store recommended items in session so customer can accept without re-asking LLM
        rec_items = []
        for pi in parsed.items:
            if pi.name:
                rec_items.append({
                    "name": pi.name,
                    "quantity": pi.quantity or 1,
                    "options": pi.options if pi.options else None,
                    "special_instructions": pi.special_instructions,
                })
        if rec_items:
            state_machine.set_context(session, "recommended_items", rec_items)
            logger.warning(
                "REC_STORED: stored %d recommended item(s) in session: %r, session_id=%s",
                len(rec_items), [r["name"] for r in rec_items], session.id,
            )
        # Do NOT add to cart — customer must accept first
        state_machine.transition_state(session, ConversationState.BROWSING_MENU.value)
        return (
            parsed.message,
            True,
            llm_response.total_tokens,
            llm_response.cost_cents,
            llm_response.provider,
        )

    if parsed.action == "ask_options":
        # LLM needs clarification (e.g. size) before adding an item.
        # Save the pending item(s) so the CHOOSING_OPTIONS handler can resolve them.
        pending = []
        for pi in (parsed.items or []):
            if pi.name:
                pending.append({
                    "name": pi.name,
                    "quantity": pi.quantity or 1,
                    "options": pi.options,
                    "special_instructions": pi.special_instructions,
                })
        if pending:
            state_machine.set_context(session, "pending_options", pending)
            logger.warning(
                "ASK_OPTIONS: saved %d pending item(s), transitioning to CHOOSING_OPTIONS. session_id=%s",
                len(pending), session.id,
            )
        state_machine.transition_state(session, ConversationState.CHOOSING_OPTIONS.value)
        return (
            parsed.message,
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

        # LLM-returned confirm_order never creates an order directly.
        # Only deterministic is_confirmation() in _handle_message may do that.
        logger.warning("LLM_CONFIRM_BLOCKED: re-prompting. session_id=%s", session.id)
        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        summary = state_machine.cart_summary_text(session, business.currency)
        total = state_machine.cart_total_cents(session)
        order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
        confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
        if order_mode == "DELIVERY":
            confirm_msg += "\n_Delivery fee will be confirmed by our team._"

        return (
            confirm_msg,
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

    # ── Order-mutation guard ─────────────────────────────────────────────
    # If the intent was an ordering/removal message but the LLM returned an
    # action that doesn't mutate the cart, we must NOT show the LLM's message —
    # it is almost certainly a fake/hallucinated order summary.
    # Covers ORDER_START, ORDER_ADD, and ORDER_REMOVE.
    _ORDER_MUTATION_ACTIONS = {"add_items", "remove_item", "replace_item", "ask_options"}
    _ORDER_INTENTS = {MessageIntent.ORDER_START, MessageIntent.ORDER_ADD, MessageIntent.ORDER_REMOVE}
    if intent in _ORDER_INTENTS and parsed.action not in _ORDER_MUTATION_ACTIONS:
        # Before firing the guard, check if the message actually contains menu items.
        # An empty cart + no item names in the message = customer is asking about ordering
        # (e.g. "Can I place an order for delivery?"), not a failed item extraction.
        # In that case let the LLM's chitchat response through — it's correct.
        _cart_is_empty = not state_machine.get_cart(session)
        _has_menu_items = bool(_extract_items_from_message(msg_text, items))
        if _cart_is_empty and not _has_menu_items:
            logger.warning(
                "ORDER_MUTATION_GUARD_SKIPPED: empty cart + no items in msg, letting LLM response through. "
                "intent=%s action=%s msg=%r",
                intent.value if intent else "None", parsed.action, msg_text[:80],
            )
            # Fall through to catch-all below — LLM's chitchat response is returned
        else:
            logger.warning(
                "ORDER_MUTATION_MISSED: intent=%s parsed_action=%s state=%s msg=%r",
                intent.value if intent else "None",
                parsed.action,
                session.state,
                msg_text[:80],
            )
            if intent == MessageIntent.ORDER_REMOVE:
                retry_msg = "Sorry, I didn't catch which item to remove. Could you tell me the name of the item you'd like taken off? 😊"
            else:
                retry_msg = "Sorry, I didn't catch the items properly. Please tell me exactly what you'd like to add 😊"
            return (
                retry_msg,
                True,
                llm_response.total_tokens,
                llm_response.cost_cents,
                llm_response.provider,
            )

    # ── Catch-all: chitchat or unknown action ────────────────────────────
    # Safety guard: warn loudly when the LLM sends conversational text while
    # the session has an active cart. This is the "fake order summary" bug path.
    # We do NOT suppress the message — it could be a legitimate clarification —
    # but the log entry provides proof of what happened.
    active_cart = state_machine.get_cart(session)
    if active_cart:
        logger.warning(
            "CHITCHAT_WITH_ACTIVE_CART: action=%r, cart_items=%d, state=%s, session_id=%s — "
            "LLM returned no cart mutation. Cart unchanged. msg_preview=%r",
            parsed.action,
            len(active_cart),
            session.state,
            session.id,
            parsed.message[:80],
        )
    return (
        parsed.message,
        True,
        llm_response.total_tokens,
        llm_response.cost_cents,
        llm_response.provider,
    )


async def _cancel_prior_live_orders(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    exclude_order_id,
) -> None:
    """
    After a new order is created, cancel any prior orders from the same
    customer that are still in the live queue (not yet in progress / done).
    This handles the common case where the customer re-orders without the
    session being in ORDER_PLACED state, so superseded_order_id was never set.
    Fire-and-forget; never raises.
    """
    _CANCELLABLE_STATUSES = [
        "PENDING_DELIVERY_FEE", "FEE_SENT", "NEW", "ACCEPTED",
    ]
    try:
        result = await db.execute(
            select(_Order).where(
                _Order.business_id == business.id,
                _Order.customer_id == customer.id,
                _Order.id != exclude_order_id,
                _Order.status.in_(_CANCELLABLE_STATUSES),
            )
        )
        old_orders = result.scalars().all()
        if not old_orders:
            return

        try:
            from backend.app.db.session import get_redis
            import json as _json
            redis = await get_redis()
        except Exception:
            redis = None

        for old_order in old_orders:
            old_order.status = "CANCELLED"
            db.add(_OrderEvent(
                order_id=old_order.id,
                business_id=business.id,
                old_status=None,
                new_status="CANCELLED",
                reason="Superseded by new customer order",
            ))
            logger.warning(
                "PRIOR_ORDER_CANCELLED: order_id=%s, old_status=%s",
                old_order.id, old_order.status,
            )
            if redis:
                try:
                    await redis.publish(
                        f"orders:{business.id}",
                        _json.dumps({
                            "type": "order_status_changed",
                            "order_id": str(old_order.id),
                            "old_status": "unknown",
                            "new_status": "CANCELLED",
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }),
                    )
                except Exception:
                    pass
        await db.flush()
    except Exception:
        logger.exception("Failed to cancel prior live orders for customer %s", customer.id)


async def _cancel_superseded_order(
    db: AsyncSession,
    business: Business,
    session,
) -> None:
    """
    If the customer modified a previously placed order, cancel the old one
    so it disappears from the live staff queue. Fire-and-forget; never raises.
    """
    superseded_id = state_machine.get_context(session, "superseded_order_id")
    if not superseded_id:
        return
    try:
        await db.execute(
            _sa_update(_Order)
            .where(
                _Order.id == _parse_uuid(superseded_id),
                _Order.business_id == business.id,
                _Order.status.not_in(["CANCELLED", "COLLECTED", "DELIVERED"]),
            )
            .values(status="CANCELLED", cancelled_reason="Superseded by customer modification")
        )
        db.add(_OrderEvent(
            order_id=_parse_uuid(superseded_id),
            business_id=business.id,
            old_status=None,
            new_status="CANCELLED",
            reason="Superseded by customer modification",
        ))
        await db.flush()
        # Publish SSE so staff dashboard removes the old card immediately
        try:
            from backend.app.db.session import get_redis
            import json as _json
            redis = await get_redis()
            await redis.publish(
                f"orders:{business.id}",
                _json.dumps({
                    "type": "order_status_changed",
                    "order_id": superseded_id,
                    "old_status": "unknown",
                    "new_status": "CANCELLED",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }),
            )
        except Exception:
            pass
        state_machine.set_context(session, "superseded_order_id", None)
        logger.warning("SUPERSEDED_ORDER_CANCELLED: order_id=%s, session_id=%s", superseded_id, session.id)
    except Exception:
        logger.exception("Failed to cancel superseded order %s", superseded_id)


async def _handle_order_confirmation(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Handle order confirmation — check details, create order."""

    # ── Ask pickup vs delivery if not yet chosen ─────────────────────────
    order_mode = state_machine.get_context(session, "order_mode", None)
    _delivery_enabled = bool(getattr(business, "delivery_enabled", False))
    _order_in_only = bool(getattr(business, "order_in_only", False))
    logger.warning(
        "ORDER_MODE_CHECK: order_mode=%s, delivery_enabled=%s, order_in_only=%s, session_id=%s",
        order_mode, _delivery_enabled, _order_in_only, session.id,
    )
    if order_mode is None:
        if _delivery_enabled and not _order_in_only:
            state_machine.transition_state(session, ConversationState.CHOOSING_ORDER_MODE.value)
            logger.warning("ORDER_MODE_GATE: asking customer, session_id=%s", session.id)
            return (
                "Is this order for *pickup* 🏃 or *delivery* 🚗?",
                False, None, None, None,
            )
        # Business only supports pickup/dine-in — set it automatically
        order_mode = "DINE_IN" if _order_in_only else "PICKUP"
        state_machine.set_context(session, "order_mode", order_mode)
        logger.warning("ORDER_MODE_AUTO: set to %s, session_id=%s", order_mode, session.id)

    already_have = {}
    if state_machine.get_context(session, "customer_name"):
        already_have["customer_name"] = True
    if state_machine.get_context(session, "phone_number") or customer.phone_number:
        already_have["phone_number"] = True
    if state_machine.get_context(session, "delivery_address"):
        already_have["delivery_address"] = True
    need_name = business.require_customer_name
    need_phone = business.require_phone_number
    # Delivery address always required for delivery orders
    need_address = order_mode == "DELIVERY" and not already_have.get("delivery_address")

    details_prompt = responses.collecting_details_response(
        need_name,
        need_phone,
        need_address,
        already_have,
    )

    if details_prompt:
        state_machine.transition_state(session, ConversationState.COLLECTING_DETAILS.value)
        return details_prompt, False, None, None, None

    # ── Delivery flow: park the order waiting for staff to set the fee ───
    if order_mode == "DELIVERY":
        delivery_address = state_machine.get_context(session, "delivery_address")
        if not delivery_address:
            # Should have been caught above, but guard again
            state_machine.transition_state(session, ConversationState.COLLECTING_DETAILS.value)
            return "Please provide your delivery address.", False, None, None, None

        # Check if a fee has already been set and approved
        fee_status = state_machine.get_context(session, "delivery_fee_status")
        if fee_status != "APPROVED":
            # Create the order in PENDING_DELIVERY_FEE status so staff can see it
            try:
                await usage_tracker.check_daily_limit(db, business, "orders")
            except DailyLimitError:
                return "Sorry, we can't accept more orders right now. Please try again later.", False, None, None, None

            await _cancel_superseded_order(db, business, session)

            # Re-lock confirmed_cart from the current live cart right before
            # creating the order. Delivery orders are created several messages
            # after the customer last said "yes" (address collection, etc.), so
            # confirmed_cart in context can lag behind a replace_item update.
            import copy as _copy_dlv
            _live_cart_dlv = state_machine.get_cart(session)
            if _live_cart_dlv:
                state_machine.set_context(
                    session, "confirmed_cart", _copy_dlv.deepcopy(_live_cart_dlv)
                )
                logger.warning(
                    "DELIVERY_CART_RELOCK: %d item(s), total_cents=%d, session_id=%s",
                    len(_live_cart_dlv),
                    sum(i["line_total_cents"] for i in _live_cart_dlv),
                    session.id,
                )

            order = await order_creator.create_order_from_cart(
                db, business, customer, session,
                initial_status="PENDING_DELIVERY_FEE",
            )
            await _cancel_prior_live_orders(db, business, customer, exclude_order_id=order.id)
            # Clear cart now — items are persisted in the order's items_json.
            # An empty cart prevents the ORDER_CONFIRM gate from hijacking any
            # subsequent "Yes" reply (which must go to WAITING_DELIVERY_FEE_APPROVAL).
            state_machine.clear_cart(session)
            state_machine.set_context(session, "pending_order_id", str(order.id))
            state_machine.transition_state(session, ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value)
            logger.warning(
                "DELIVERY_FEE_PENDING: order_id=%s, address=%r, session_id=%s",
                order.id, delivery_address, session.id,
            )
            return (
                "Got it! 📍 Your delivery address has been noted.\n\n"
                "Our team will review your location and send you the delivery fee shortly. "
                "You'll receive a WhatsApp message to confirm before we process your order. 🚗",
                False, None, None, None,
            )

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

    # ── Cancel the superseded order (customer replaced a prior order) ────
    await _cancel_superseded_order(db, business, session)

    # Re-lock confirmed_cart from the current live cart right before creating
    # the order. This handles the case where replace_item updated the live cart
    # (e.g., swapped Wings 12 pcs → 6 pcs) but the DB-persisted confirmed_cart
    # may still reflect the older snapshot from a prior "yes" lock.
    import copy as _copy_pu
    _live_cart_pu = state_machine.get_cart(session)
    if _live_cart_pu:
        state_machine.set_context(
            session, "confirmed_cart", _copy_pu.deepcopy(_live_cart_pu)
        )
        logger.warning(
            "PICKUP_CART_RELOCK: %d item(s), total_cents=%d, session_id=%s",
            len(_live_cart_pu),
            sum(i["line_total_cents"] for i in _live_cart_pu),
            session.id,
        )

    order = await order_creator.create_order_from_cart(db, business, customer, session)
    await _cancel_prior_live_orders(db, business, customer, exclude_order_id=order.id)

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


async def _handle_recommendation_acceptance(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    recommended_items: list[dict],
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    Customer accepted the bot's recommendation.
    Convert stored recommended_items into real cart entries using add_to_cart,
    then lock the cart and go to CONFIRMING_ORDER.
    """
    _, items = await _load_menu(db, business.id)
    items_map = {i.name.lower(): i for i in items if i.is_active and not i.is_deleted}

    added: list[str] = []
    unmatched: list[str] = []

    for rec in recommended_items:
        name = rec.get("name", "")
        matched = items_map.get(name.lower())
        if not matched:
            # Fuzzy fallback
            candidates = [
                (len(mn), mi) for mn, mi in items_map.items()
                if mn in name.lower() and len(mn) / max(len(name), 1) >= 0.5
            ]
            if candidates:
                matched = sorted(candidates, reverse=True)[0][1]

        if matched:
            safe_qty = max(1, min(int(rec.get("quantity") or 1), 20))
            state_machine.add_to_cart(
                session,
                menu_item_id=str(matched.id),
                name=matched.name,
                price_cents=matched.price_cents,
                quantity=safe_qty,
                options=rec.get("options"),
                special_instructions=rec.get("special_instructions"),
            )
            added.append(f"{safe_qty}x {matched.name}")
            logger.warning(
                "REC_ACCEPT_ADDED: %dx %s (price_cents=%d), session_id=%s",
                safe_qty, matched.name, matched.price_cents, session.id,
            )
        else:
            unmatched.append(name)
            logger.warning(
                "REC_ACCEPT_UNMATCHED: %r not found in menu, session_id=%s",
                name, session.id,
            )

    # Clear the stored recommendation regardless of outcome
    state_machine.set_context(session, "recommended_items", None)

    if not added:
        return (
            "Sorry, I couldn't find those items on the menu. Say *\"menu\"* to browse what's available.",
            False, None, None, None,
        )

    # Lock cart and move to CONFIRMING_ORDER
    import copy as _copy
    updated_cart = state_machine.get_cart(session)
    state_machine.set_context(session, "confirmed_cart", _copy.deepcopy(updated_cart))
    state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
    logger.warning(
        "REC_ACCEPT_CART_LOCKED: items=%d, total_cents=%d, session_id=%s",
        len(updated_cart), sum(i["line_total_cents"] for i in updated_cart), session.id,
    )

    order_mode = state_machine.get_context(session, "order_mode", "PICKUP")
    total = state_machine.cart_total_cents(session)
    summary = state_machine.cart_summary_text(session, business.currency)
    confirm_msg = responses.ask_confirmation_response(summary, total, 0, order_mode, business.currency)
    if order_mode == "DELIVERY":
        confirm_msg += "\n_Delivery fee will be confirmed by our team._"

    parts = ["Added to your order: " + ", ".join(added) + " ✅"]
    if unmatched:
        parts.append(f"Sorry, I couldn't find: {', '.join(unmatched)}.")
    parts.append("\n" + confirm_msg)

    return ("\n".join(parts), False, None, None, None)


async def _handle_choosing_order_mode(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Parse customer's pickup/delivery choice and proceed to confirmation."""
    text_lower = msg_text.lower().strip()

    if any(w in text_lower for w in ["pickup", "pick up", "pick-up", "collect", "collection", "takeaway", "take away", "take-away"]):
        state_machine.set_context(session, "order_mode", "PICKUP")
        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        logger.warning("ORDER_MODE_SET: PICKUP, session_id=%s", session.id)
        return await _handle_order_confirmation(db, business, customer, session)

    if any(w in text_lower for w in ["delivery", "deliver", "bring", "drop", "drop off"]):
        state_machine.set_context(session, "order_mode", "DELIVERY")
        state_machine.transition_state(session, ConversationState.CONFIRMING_ORDER.value)
        logger.warning("ORDER_MODE_SET: DELIVERY, session_id=%s", session.id)
        return await _handle_order_confirmation(db, business, customer, session)

    return (
        "Please choose: *pickup* 🏃 (collect from us) or *delivery* 🚗 (we bring it to you)?",
        False, None, None, None,
    )


async def _handle_waiting_delivery_fee(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    Customer is replying to a delivery fee proposal sent by staff.
    YES → mark fee approved, finalize order.
    NO  → cancel order, offer alternatives.
    Anything else → re-prompt.
    """
    fee_cents = state_machine.get_context(session, "delivery_fee_cents", 0)
    pending_order_id = state_machine.get_context(session, "pending_order_id")

    if intent_router.is_confirmation(msg_text):
        # Fee approved — now ask how they'll pay before finalising.
        state_machine.set_context(session, "delivery_fee_status", "APPROVED")
        state_machine.transition_state(session, ConversationState.COLLECTING_PAYMENT.value)
        logger.warning("DELIVERY_FEE_APPROVED: moving to COLLECTING_PAYMENT, session_id=%s", session.id)
        return (
            "Great! Will you be paying *cash* 💵 or *card* 💳?",
            False, None, None, None,
        )

    if intent_router.is_negation(msg_text):
        # Cancel the pending order
        if pending_order_id:
            try:
                await db.execute(
                    _sa_update(_Order)
                    .where(
                        _Order.id == _parse_uuid(pending_order_id),
                        _Order.business_id == business.id,
                    )
                    .values(status="CANCELLED", cancelled_reason="Customer rejected delivery fee")
                )
                db.add(_OrderEvent(
                    order_id=_parse_uuid(pending_order_id),
                    business_id=business.id,
                    old_status="PENDING_DELIVERY_FEE",
                    new_status="CANCELLED",
                    reason="Customer rejected delivery fee",
                ))
                await db.flush()
            except Exception:
                logger.exception("Failed to cancel delivery order. pending_order_id=%s", pending_order_id)
        state_machine.clear_cart(session)
        state_machine.transition_state(session, ConversationState.IDLE.value)
        return (
            "No problem! Your order has been cancelled. 🗑️\n\n"
            "You can:\n• Place a pickup order instead\n• Message us to arrange delivery manually",
            False, None, None, None,
        )

    # No clear yes/no — re-prompt with the fee so they can decide
    from shared.utils.money import format_currency
    fee_str = format_currency(fee_cents, business.currency) if fee_cents else "not yet confirmed"
    return (
        f"Your delivery fee is *{fee_str}*. Do you accept?\n"
        f"Reply *yes* to confirm or *no* to cancel.",
        False, None, None, None,
    )


async def _handle_collecting_payment(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    Customer is providing their payment method (cash / card).
    Parse → store → then check if we still need name/phone → finalise order.
    """
    text_lower = msg_text.lower().strip()
    if any(w in text_lower for w in ["cash", "notes", "money"]):
        payment_method = "CASH_ON_COLLECTION"
    elif any(w in text_lower for w in ["card", "eft", "tap", "swipe", "visa", "mastercard", "credit", "debit"]):
        payment_method = "CARD"
    else:
        return (
            "Please reply *cash* 💵 or *card* 💳 so we can prepare for your arrival.",
            False, None, None, None,
        )

    state_machine.set_context(session, "payment_method", payment_method)
    logger.warning(
        "PAYMENT_METHOD_SET: %s, session_id=%s",
        payment_method, session.id,
    )

    # Check if we still need name/phone (may already be set)
    ctx = session.context_json or {}
    already_have = {}
    if ctx.get("customer_name") or customer.display_name:
        already_have["customer_name"] = True
    if ctx.get("phone_number") or customer.phone_number:
        already_have["phone_number"] = True

    details_prompt = responses.collecting_details_response(
        business.require_customer_name,
        business.require_phone_number,
        False,  # delivery address already collected earlier
        already_have,
    )
    if details_prompt:
        state_machine.transition_state(session, ConversationState.COLLECTING_DETAILS.value)
        return details_prompt, False, None, None, None

    return await _finalize_pending_delivery_order(db, business, customer, session)


async def _finalize_pending_delivery_order(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    Finalise a PENDING_DELIVERY_FEE or FEE_SENT order after the customer has
    approved the fee and provided their payment method.
    Updates the order: sets fee, total, payment_status, status → NEW.
    """
    from shared.utils.money import format_currency

    fee_cents = state_machine.get_context(session, "delivery_fee_cents", 0)
    pending_order_id = state_machine.get_context(session, "pending_order_id")
    payment_method = state_machine.get_context(session, "payment_method", "CASH_ON_COLLECTION")
    payment_status = "CASH_ON_COLLECTION" if payment_method == "CASH_ON_COLLECTION" else "PENDING"

    if pending_order_id:
        try:
            result = await db.execute(
                _sa_update(_Order)
                .where(
                    _Order.id == _parse_uuid(pending_order_id),
                    _Order.business_id == business.id,
                )
                .values(
                    delivery_fee_cents=fee_cents,
                    total_cents=_Order.subtotal_cents + fee_cents,
                    status="NEW",
                    payment_status=payment_status,
                )
                .returning(_Order.order_number, _Order.subtotal_cents)
            )
            row = result.one_or_none()
            order_number = row.order_number if row else "—"
            subtotal = row.subtotal_cents if row else 0

            db.add(_OrderEvent(
                order_id=_parse_uuid(pending_order_id),
                business_id=business.id,
                old_status="FEE_SENT",
                new_status="NEW",
                reason="Customer approved delivery fee and provided payment method",
            ))
            await db.flush()

            # Publish SSE so staff dashboard refreshes
            try:
                from backend.app.db.session import get_redis
                import json as _json
                redis = await get_redis()
                await redis.publish(
                    f"orders:{business.id}",
                    _json.dumps({
                        "type": "order_created",
                        "order_id": pending_order_id,
                        "order_number": order_number,
                        "status": "NEW",
                        "total_cents": subtotal + fee_cents,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }),
                )
            except Exception:
                pass

            state_machine.clear_cart(session)
            state_machine.transition_state(session, ConversationState.ORDER_PLACED.value)
            await usage_tracker.increment_usage(
                db, business.id, business.timezone,
                orders_created=1, revenue_cents=subtotal + fee_cents,
            )
            return (
                f"✅ *Order Confirmed!*\n\nOrder Number: *{order_number}*\n"
                f"Delivery fee: {format_currency(fee_cents, business.currency)}\n"
                f"💰 *Total: {format_currency(subtotal + fee_cents, business.currency)}*\n"
                f"Payment: *{'Cash on delivery' if payment_method == 'CASH_ON_COLLECTION' else 'Card'}*\n\n"
                f"🚗 We'll be on our way once your order is ready!",
                False, None, None, None,
            )
        except Exception:
            logger.exception("Failed to finalise delivery order. pending_order_id=%s", pending_order_id)
            return "Something went wrong confirming your order. Please contact us directly.", False, None, None, None

    return "✅ Delivery confirmed! Your order is being prepared.", False, None, None, None


def _parse_name_and_phone(text: str) -> tuple[str | None, str | None]:
    """
    Split a free-text message into (name, phone).  Handles multi-line input:
      "Mohammed Moosa\\n0837866021" → ("Mohammed Moosa", "0837866021")
      "0837866021\\nMohammed Moosa" → ("Mohammed Moosa", "0837866021")
      "Mohammed Moosa"             → ("Mohammed Moosa", None)
      "0837866021"                 → (None, "0837866021")
    A line is treated as a phone if, after stripping non-digit/+ chars, it is
    9–15 characters long.
    """
    import re as _re
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None, None

    phone: str | None = None
    name_parts: list[str] = []

    for line in lines:
        cleaned = _re.sub(r"[^\d+]", "", line)
        if 9 <= len(cleaned) <= 15 and phone is None:
            phone = cleaned
        else:
            name_parts.append(line)

    name = " ".join(name_parts).strip() or None
    return name, phone


async def _handle_collecting_details(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
    msg_text: str,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """Parse customer details from free-text message during COLLECTING_DETAILS state."""
    import re

    ctx = session.context_json or {}
    text = msg_text.strip()

    # ── Collect name ─────────────────────────────────────────────────────────
    if business.require_customer_name and "customer_name" not in ctx:
        parsed_name, parsed_phone = _parse_name_and_phone(text)

        if not parsed_name:
            # Only digits were sent when we expected a name — re-prompt
            return (
                "Please send your name (e.g. *Mohammed Moosa*).",
                False, None, None, None,
            )

        state_machine.set_context(session, "customer_name", parsed_name)
        customer.display_name = parsed_name

        # Opportunistically capture phone if sent on the same message
        if (
            parsed_phone
            and business.require_phone_number
            and "phone_number" not in (session.context_json or {})
        ):
            state_machine.set_context(session, "phone_number", parsed_phone)
            customer.phone_number = parsed_phone

        still_missing = _check_missing_details(business, session)
        if still_missing:
            return still_missing, False, None, None, None
        return await _next_after_details(db, business, customer, session)

    # ── Collect phone ─────────────────────────────────────────────────────────
    if business.require_phone_number and "phone_number" not in ctx:
        phone = re.sub(r"[^\d+]", "", text)
        if len(phone) >= 9:
            state_machine.set_context(session, "phone_number", phone)
            customer.phone_number = phone
            still_missing = _check_missing_details(business, session)
            if still_missing:
                return still_missing, False, None, None, None
            return await _next_after_details(db, business, customer, session)
        return "Please send a valid phone number (e.g., 0812345678).", False, None, None, None

    # ── Collect delivery address ──────────────────────────────────────────────
    if business.require_delivery_address and "delivery_address" not in ctx:
        state_machine.set_context(session, "delivery_address", text)
        still_missing = _check_missing_details(business, session)
        if still_missing:
            return still_missing, False, None, None, None
        return await _next_after_details(db, business, customer, session)

    return await _next_after_details(db, business, customer, session)


async def _next_after_details(
    db: AsyncSession,
    business: Business,
    customer: Customer,
    session,
) -> tuple[str, bool, int | None, int | None, str | None]:
    """
    After all required details are collected, decide what to do next:
    - If a pending delivery order exists (fee already approved), finalise it.
    - Otherwise, continue normal order confirmation flow.
    """
    if state_machine.get_context(session, "pending_order_id"):
        return await _finalize_pending_delivery_order(db, business, customer, session)
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


async def _load_conversation_history(
    db: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    limit: int = 8,
) -> list[dict]:
    """
    Load the last N messages for this customer as LLM conversation history.
    Returns messages in chronological order, formatted as role/content dicts.
    """
    result = await db.execute(
        select(Message.direction, Message.text)
        .where(
            Message.business_id == business_id,
            Message.customer_id == customer_id,
            Message.text.is_not(None),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    rows = list(reversed(result.all()))
    return [
        {
            "role": "user" if row.direction == "INBOUND" else "assistant",
            "content": row.text,
        }
        for row in rows
        if row.text
    ]


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