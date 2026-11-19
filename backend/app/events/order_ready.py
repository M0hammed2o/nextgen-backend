"""
Order-ready WhatsApp message for souk event vendors (BUG-0210).

A walk-up stall customer has usually never messaged the vendor, so Meta only
allows a pre-approved TEMPLATE (free text outside the 24-hour customer-service
window is rejected, error 131047). This module sends the READY message for an
event vendor's POS order:

- only with the customer's recorded notify_when_ready consent (checked by the
  caller, routes_orders.update_order_status) and never to an opted-out
  customer;
- through the vendor's OWN WhatsApp connection (send_template_message with
  db= and business=, so token_resolver applies; an event vendor never gets the
  platform token, so Meta bills the vendor);
- with the template the vendor submitted in its own WhatsApp Manager
  (Business.whatsapp_ready_template_name/_language), only once a SUPER_ADMIN
  has set whatsapp_ready_template_approved. NextGen never creates or edits a
  template through the Meta API;
- without an approved template: free text only if the customer messaged this
  business within the last 24 hours (Customer.last_message_at), otherwise
  nothing is sent;
- at most one READY message per order. Every outcome is recorded as an
  OrderEvent on the order, so staff can see whether the customer was told.

Restaurants are NOT routed here: their READY message is unchanged (free text,
routes_orders._customer_status_message). Switching restaurants to the template
path is possible later without changing behaviour when no template is set,
but is deliberately not done in this change (docs/EVENT_WHATSAPP.md).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.event import Event, EventVendor
from shared.models.order import Order, OrderEvent

logger = logging.getLogger("nextgen.events.order_ready")

# What the vendor submits in WhatsApp Manager (documented in docs/EVENT_WHATSAPP.md).
SUGGESTED_TEMPLATE_NAME = "order_ready_v1"
SUGGESTED_TEMPLATE_LANGUAGE = "en_US"
SUGGESTED_TEMPLATE_CATEGORY = "UTILITY"
SUGGESTED_TEMPLATE_BODY = "Hi! Your order {{1}} from {{2}} is ready for collection at {{3}}."

CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)

# OrderEvent reasons (also the idempotency marker for "already told").
REASON_SENT_TEMPLATE = "Order-ready WhatsApp sent (approved template)"
REASON_SENT_FREE_TEXT = "Order-ready WhatsApp sent (free text, customer messaged within 24h)"
REASON_NOT_SENT_NO_TEMPLATE = "Order-ready WhatsApp not sent: no approved template"
REASON_NOT_SENT_OPTED_OUT = "Order-ready WhatsApp not sent: customer opted out"
REASON_NOT_SENT_UNAVAILABLE = "Order-ready WhatsApp not sent: WhatsApp not available for this vendor"
REASON_NOT_SENT_NO_NUMBER = "Order-ready WhatsApp not sent: no usable customer number"
REASON_SEND_FAILED = "Order-ready WhatsApp send failed"
_SENT_REASONS = (REASON_SENT_TEMPLATE, REASON_SENT_FREE_TEXT)

# Readiness states (manager + staff UI).
STATE_NOT_EVENT = "NOT_EVENT"
STATE_AWAITING_ADMIN = "AWAITING_ADMIN_APPROVAL"  # is_whatsapp_enabled is off
STATE_NOT_CONNECTED = "NOT_CONNECTED"             # no usable own connection
STATE_TEMPLATE_PENDING = "TEMPLATE_PENDING"       # connected, template not confirmed
STATE_READY = "READY"


@dataclass(frozen=True)
class ReadyNotifyStatus:
    state: str
    available: bool
    whatsapp_enabled: bool
    connection_status: str | None
    template_name: str | None
    template_language: str | None
    template_approved: bool


async def ready_notify_status(db: AsyncSession, business: Business) -> ReadyNotifyStatus:
    """
    Whether this event vendor can send the order-ready message right now, and
    what is missing. Only this business's own rows are read.
    """
    from backend.app.whatsapp.token_resolver import resolve_whatsapp_credentials
    from shared.models.whatsapp_connection import WhatsAppConnection

    name = business.whatsapp_ready_template_name or None
    language = business.whatsapp_ready_template_language or None
    approved = bool(business.whatsapp_ready_template_approved and name)
    enabled = bool(business.is_whatsapp_enabled)
    if business.plan != EVENT_PLAN:
        return ReadyNotifyStatus(STATE_NOT_EVENT, False, enabled, None, name, language, approved)
    connection_status = (await db.execute(
        select(WhatsAppConnection.status).where(WhatsAppConnection.business_id == business.id)
    )).scalar_one_or_none()
    if not enabled:
        state = STATE_AWAITING_ADMIN
    elif await resolve_whatsapp_credentials(db, business) is None:
        state = STATE_NOT_CONNECTED
    elif not approved:
        state = STATE_TEMPLATE_PENDING
    else:
        state = STATE_READY
    return ReadyNotifyStatus(state, state == STATE_READY, enabled, connection_status, name, language, approved)


async def _stall_label(db: AsyncSession, business_id) -> str:
    rows = (await db.execute(
        select(EventVendor.stall_code, Event.name, Event.status)
        .join(Event, Event.id == EventVendor.event_id)
        .where(EventVendor.business_id == business_id, EventVendor.status != "CANCELLED",
               Event.status != "CANCELLED")
        .order_by(Event.created_at)
    )).all()
    if not rows:
        return "the stall"
    open_rows = [r for r in rows if r.status == "OPEN"] or rows
    stall, event_name, _ = open_rows[0]
    return f"stall {stall}, {event_name}" if stall else event_name


def _record(db: AsyncSession, order: Order, reason: str) -> None:
    db.add(OrderEvent(
        order_id=order.id, business_id=order.business_id, old_status=order.status, new_status=order.status,
        changed_by_user_id=None, reason=reason,
    ))


async def send_event_order_ready(db: AsyncSession, business: Business, order: Order, wa_id: str | None) -> str:
    """
    Send (or decide not to send) the READY message for an event vendor's
    consented POS order. Returns the OrderEvent reason recorded. Commits its
    own OrderEvent (the caller's status change is already committed). Never
    raises: a messaging problem must not fail the status change.

    The work runs inside a SAVEPOINT, so a failure only undoes this module's
    own writes and never rolls back (and expires) the caller's committed
    order, which the route still serialises afterwards (review L-3).
    """
    try:
        async with db.begin_nested():
            reason = await _decide_and_send(db, business, order, wa_id)
            if reason != "already sent":
                _record(db, order, reason)
    except Exception:  # noqa: BLE001
        logger.exception("EVENT_ORDER_READY_FAILED: order_id=%s", getattr(order, "id", None))
        reason = REASON_SEND_FAILED
        try:
            async with db.begin_nested():
                _record(db, order, reason)
        except Exception:  # noqa: BLE001
            logger.exception("EVENT_ORDER_READY_RECORD_FAILED: order_id=%s", getattr(order, "id", None))
    try:
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("EVENT_ORDER_READY_COMMIT_FAILED: order_id=%s", getattr(order, "id", None))
        await db.rollback()
        await db.refresh(order)  # the caller's committed order stays loaded
    logger.info("EVENT_ORDER_READY: order_id=%s outcome=%s", getattr(order, "id", None), reason)
    return reason


async def _decide_and_send(db: AsyncSession, business: Business, order: Order, wa_id: str | None) -> str:
    """Decide, send if allowed, and return the outcome reason (may raise)."""
    from backend.app.bot.whatsapp_sender import send_notification_message, send_template_message

    already = (await db.execute(
        select(OrderEvent.id).where(
            OrderEvent.order_id == order.id, OrderEvent.business_id == order.business_id,
            OrderEvent.reason.in_(_SENT_REASONS),
        ).limit(1)
    )).scalar_one_or_none()
    if already is not None:
        return "already sent"

    customer = None
    if order.customer_id:
        customer = (await db.execute(select(Customer).where(
            Customer.id == order.customer_id, Customer.business_id == order.business_id))).scalar_one_or_none()
    if customer is None and wa_id:
        customer = (await db.execute(select(Customer).where(
            Customer.business_id == order.business_id, Customer.wa_id == wa_id))).scalar_one_or_none()
    if customer is not None and customer.opted_out:
        reason = REASON_NOT_SENT_OPTED_OUT
    elif not wa_id:
        reason = REASON_NOT_SENT_NO_NUMBER
    else:
        status = await ready_notify_status(db, business)
        if status.state in (STATE_AWAITING_ADMIN, STATE_NOT_CONNECTED):
            reason = REASON_NOT_SENT_UNAVAILABLE
        elif status.template_approved:
            vendor = business.brand_display_name or business.name
            components = [{"type": "body", "parameters": [
                {"type": "text", "text": order.order_number},
                {"type": "text", "text": vendor},
                {"type": "text", "text": await _stall_label(db, business.id)},
            ]}]
            wamid, _err = await send_template_message(
                phone_number_id=business.whatsapp_phone_number_id, recipient_wa_id=wa_id,
                template_name=status.template_name, language_code=status.template_language or "en_US",
                components=components, db=db, business=business,
                customer_id=getattr(customer, "id", None), intent="ORDER_READY",
            )
            reason = REASON_SENT_TEMPLATE if wamid else REASON_SEND_FAILED
        else:
            last = getattr(customer, "last_message_at", None)
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last is not None and datetime.now(timezone.utc) - last <= CUSTOMER_SERVICE_WINDOW:
                wamid = await send_notification_message(
                    phone_number_id=business.whatsapp_phone_number_id, recipient_wa_id=wa_id,
                    text=f"🎉 Your order *{order.order_number}* is ready for collection!",
                    db=db, business=business,
                    order_id=order.id, business_id=order.business_id, order_status=order.status,
                )
                reason = REASON_SENT_FREE_TEXT if wamid else REASON_SEND_FAILED
            else:
                reason = REASON_NOT_SENT_NO_TEMPLATE
    return reason
