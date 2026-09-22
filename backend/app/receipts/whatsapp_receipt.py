"""
Digital receipt delivery — the single entry point every trigger (payment
webhook, cash-on-collection status transition, POS till, manager-dashboard
resend) calls: `send_digital_receipt()`.

REPLACES THE OVERNIGHT FREE-FORM MECHANISM
--------------------------------------------
The 2026-09-22 overnight pass (see WORKLOG.md, TODO.md) sent a full
itemised receipt as FREE-FORM WhatsApp text via send_text_message(), gated
on the customer's inbound message still being inside Meta's 24-hour
customer-service window. That is not sufficient for production: a receipt
must be deliverable "regardless of whether a 24-hour customer-service
window exists" — which Meta only allows via a pre-approved TEMPLATE
message, not free text. This module now sends ONLY a template (see
send_template_message in backend/app/bot/whatsapp_sender.py), pointing the
customer at the public receipt page (backend/app/api/v1/
routes_receipt_public.py) for the actual itemised content — the template
carries three parameters (restaurant name, amount, receipt URL), matching
Meta's requirement that template body text be fixed and pre-approved.

The old build_whatsapp_receipt_text()/send_whatsapp_receipt() free-text
renderer has been removed entirely (not left running in parallel) — see
DECISIONS.md / this change's commit message. A receipt is still built from
exactly one source of truth, backend/app/receipts/document.py::
build_receipt_document(); the itemised content now lives on the public
HTML page (backend/app/receipts/html_public.py) rather than in the
WhatsApp message body itself.

DURABLE RECORD: DigitalReceipt, NOT A SECOND OUTBOX
------------------------------------------------------
Exactly one DigitalReceipt row exists per order (UNIQUE order_id — see
shared/models/digital_receipt.py's own docstring for why this is not a
reuse of message_outbox). Its whatsapp_status IS the durable delivery
record: SENT/DELIVERED means done, FAILED means retryable via an explicit
staff-triggered resend (POS or manager dashboard), never an automatic
background retry loop.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.receipts.document import build_receipt_document
from backend.app.receipts.token import generate_receipt_token
from backend.app.utils.phone import normalize_sa_mobile
from shared.models.customer import Customer
from shared.models.digital_receipt import DigitalReceipt

logger = logging.getLogger("nextgen.receipts.whatsapp")
settings = get_settings()

# Roles allowed to force a resend of an already-SENT/DELIVERED receipt.
# Enforced at the ROUTE layer (POS / manager-dashboard endpoints), never
# inside this function — see the module docstring on why.
FORCE_RESEND_ROLES = frozenset({"OWNER", "MANAGER"})

_DONE_STATUSES = frozenset({"SENT", "DELIVERED"})


def _to_wa_id(e164: str) -> str:
    """
    Meta's Cloud API 'to' field is expected in the same digits-only format
    every inbound customer.wa_id already arrives in (confirmed:
    backend/app/api/v1/routes_webhook.py reads `message.get("from", "")`,
    which Meta never prefixes with '+'). Rather than guess whether a '+'
    prefix is tolerated, this strips it to match the one format already
    known to work.
    """
    return e164[1:] if e164.startswith("+") else e164


def _resolve_whatsapp_destination(
    *, phone_number_e164: str | None, customer: Customer | None,
) -> tuple[str | None, str | None]:
    """
    Decide who to send to and what (if anything) to record on
    DigitalReceipt.phone_number_e164.

    Priority: an explicitly supplied, already-normalised phone_number_e164
    (POS staff-typed number, or a caller resolving the order's own captured
    number) > customer.wa_id (Meta's own identity, used as-is, no
    normalisation possible or needed) > customer.phone_number (normalised
    here as a last resort, since a POS-linked Customer may only ever have
    been given a raw typed phone_number — see
    backend/app/loyalty/engine.py::get_or_create_customer_by_phone).

    Returns (wa_id_to_send_to, phone_number_e164_to_persist) — either may
    be None. wa_id_to_send_to is None when nothing resolvable.
    """
    if phone_number_e164:
        return _to_wa_id(phone_number_e164), phone_number_e164

    if customer is not None:
        wa_id = getattr(customer, "wa_id", None)
        if wa_id:
            return wa_id, None
        raw_phone = getattr(customer, "phone_number", None)
        if raw_phone:
            normalised = normalize_sa_mobile(raw_phone)
            if normalised:
                return _to_wa_id(normalised), normalised

    return None, None


async def resolve_receipt_customer_and_phone(
    db: AsyncSession, order, business_id: uuid.UUID, explicit_phone_e164: str | None,
) -> tuple[Customer | None, str | None]:
    """
    Shared resolution used by both the POS and manager-dashboard receipt
    endpoints (routes_pos.py / routes_orders.py): prefer an explicitly
    staff-typed phone number, else the order's own ALREADY-LINKED Customer
    (loaded, never created), else the order's own captured phone_number
    (best-effort normalised). Deliberately never calls
    get_or_create_customer_by_phone — a Customer row must never be created
    solely to deliver a receipt (see DigitalReceipt.customer_id's own
    docstring).
    """
    customer: Customer | None = None
    if getattr(order, "customer_id", None):
        result = await db.execute(
            select(Customer).where(
                Customer.id == order.customer_id, Customer.business_id == business_id,
            )
        )
        customer = result.scalar_one_or_none()

    phone = explicit_phone_e164
    if phone is None and customer is None and getattr(order, "phone_number", None):
        phone = normalize_sa_mobile(order.phone_number)

    return customer, phone


async def _get_or_create_receipt(db: AsyncSession, order, business) -> DigitalReceipt:
    """
    Row-locked get-or-create — the FIRST half of the guarantee that two
    concurrent requests for the same order's receipt can never create two
    rows or send WhatsApp twice. The SECOND half is that the caller
    (send_digital_receipt) does not commit — and therefore does not release
    this lock — until its entire operation, including the external
    WhatsApp send, is finished. A `SELECT ... FOR UPDATE` on an existing
    row blocks a concurrent second caller until the first's transaction
    ends; a fresh INSERT on order_id (UNIQUE) makes Postgres block a
    concurrent second INSERT the same way, only surfacing as an
    IntegrityError once the first transaction actually commits (never a
    silent double-insert). This is why the insert path below only
    `flush()`es, never commits — see send_digital_receipt's single commit
    point.
    """
    result = await db.execute(
        select(DigitalReceipt).where(DigitalReceipt.order_id == order.id).with_for_update()
    )
    receipt = result.scalar_one_or_none()
    if receipt is not None:
        return receipt

    for attempt in range(2):
        receipt = DigitalReceipt(
            business_id=business.id,
            order_id=order.id,
            token=generate_receipt_token(),
        )
        db.add(receipt)
        try:
            await db.flush()
            return receipt
        except IntegrityError:
            # Rolling back here only ever discards THIS insert attempt —
            # both real trigger points (_mark_order_paid, update_order_
            # status) have already committed their own order-state changes
            # before reaching send_digital_receipt, and the staff-facing
            # routes have no earlier uncommitted work in the same request.
            await db.rollback()
            # Either our own token collided (astronomically unlikely — see
            # token.py) or a concurrent request for the SAME order won the
            # race and its transaction has since committed (this
            # IntegrityError could only have surfaced once it did). Use
            # that row.
            result = await db.execute(
                select(DigitalReceipt).where(DigitalReceipt.order_id == order.id).with_for_update()
            )
            receipt = result.scalar_one_or_none()
            if receipt is not None:
                return receipt
            if attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable: digital receipt get-or-create exhausted retries")


async def send_digital_receipt(
    db: AsyncSession,
    order,
    business,
    *,
    channels: set[str],
    phone_number_e164: str | None = None,
    customer: Customer | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    force_resend: bool = False,
) -> DigitalReceipt:
    """
    Request (and, for WHATSAPP, attempt) delivery of order's digital
    receipt. Idempotent per order — see _get_or_create_receipt — and safe
    to call from every trigger point without risk of a duplicate WhatsApp
    send.

    channels: any of {"PRINT", "WHATSAPP"}.
      - "PRINT" only records that printing was requested (client-side till
        printing is unchanged, this is audit only) — never gated by
        business.digital_receipts_enabled.
      - "WHATSAPP" resolves a destination (see _resolve_whatsapp_
        destination), and, if business.digital_receipts_enabled and a
        destination and phone_number_id are resolvable, sends a template
        message via send_template_message(). Already-SENT/DELIVERED is a
        no-op UNLESS force_resend=True (role-checked by the CALLER, not
        here — see FORCE_RESEND_ROLES). A FAILED status is always
        retryable by calling this again (with or without force_resend,
        since FAILED never counts as "already done").

    Never raises for any EXPECTED failure mode (disabled entitlement, no
    resolvable phone, no phone_number_id configured, a classified Meta
    error) — each sets whatsapp_status=FAILED with a safe reason instead.
    A genuinely unexpected error (e.g. a DB failure) is allowed to
    propagate; both fire-and-forget call sites (_mark_order_paid,
    update_order_status) already wrap their notification block in a broad
    try/except, and the staff-facing routes surface it as a real error
    rather than silently swallowing it.

    SINGLE COMMIT POINT: this function commits exactly once, at the very
    end (see the `return await _finish()` calls below) — never earlier.
    _get_or_create_receipt only flushes. Together with its row lock (or,
    for a fresh row, Postgres's own UNIQUE-constraint blocking on a
    concurrent INSERT), this means a concurrent second call for the SAME
    order_id genuinely blocks until this call's entire operation —
    including the external WhatsApp send — is committed, which is what
    guarantees "exactly one send attempt" under concurrency, not just
    "exactly one row".
    """
    receipt = await _get_or_create_receipt(db, order, business)

    async def _finish() -> DigitalReceipt:
        await db.commit()
        await db.refresh(receipt)
        return receipt

    if customer is not None and receipt.customer_id is None:
        receipt.customer_id = getattr(customer, "id", None)
    if requested_by_user_id is not None:
        receipt.requested_by_user_id = requested_by_user_id

    if "PRINT" in channels:
        receipt.print_requested = True

    if "WHATSAPP" in channels:
        receipt.whatsapp_requested = True

        if receipt.whatsapp_status in _DONE_STATUSES and not force_resend:
            return await _finish()

        if not getattr(business, "digital_receipts_enabled", False):
            # Entitlement gates the WHATSAPP channel only — print_requested
            # above (if also set) is unaffected, and whatsapp_status is
            # left as-is (NOT_REQUESTED on a first-ever call) rather than
            # being marked FAILED, since this isn't really a delivery
            # failure — the feature is simply off for this tenant.
            return await _finish()

        destination_wa_id, resolved_phone = _resolve_whatsapp_destination(
            phone_number_e164=phone_number_e164, customer=customer,
        )
        if resolved_phone and not receipt.phone_number_e164:
            receipt.phone_number_e164 = resolved_phone

        if destination_wa_id is None:
            receipt.whatsapp_status = "FAILED"
            receipt.whatsapp_failure_reason = "no phone number available"
            return await _finish()

        phone_number_id = getattr(business, "whatsapp_phone_number_id", None)
        if not phone_number_id:
            receipt.whatsapp_status = "FAILED"
            receipt.whatsapp_failure_reason = "business has no whatsapp_phone_number_id configured"
            return await _finish()

        document = build_receipt_document(order, business)
        receipt_url = f"{settings.BACKEND_PUBLIC_URL}{settings.API_V1_PREFIX}/r/{receipt.token}"
        config = getattr(business, "receipt_config_json", None) or {}
        restaurant_name = config.get("trading_name") or getattr(business, "name", "") or ""
        template_name = (
            getattr(business, "whatsapp_receipt_template_name", None)
            or settings.RECEIPT_WHATSAPP_TEMPLATE_NAME
        )
        language_code = (
            getattr(business, "whatsapp_receipt_template_language", None)
            or settings.RECEIPT_WHATSAPP_TEMPLATE_LANGUAGE
        )
        components = [{
            "type": "body",
            "parameters": [
                {"type": "text", "text": restaurant_name},
                {"type": "text", "text": document.totals.total_display},
                {"type": "text", "text": receipt_url},
            ],
        }]

        receipt.whatsapp_status = "QUEUED"
        await db.flush()

        from backend.app.bot.whatsapp_sender import send_template_message

        wamid, error_info = await send_template_message(
            phone_number_id=phone_number_id,
            recipient_wa_id=destination_wa_id,
            template_name=template_name,
            language_code=language_code,
            components=components,
            db=db,
            business=business,
            business_id=business.id,
            customer_id=getattr(customer, "id", None),
            intent="receipt",
        )

        if wamid:
            receipt.whatsapp_status = "SENT"
            receipt.whatsapp_wamid = wamid
            receipt.whatsapp_sent_at = datetime.now(timezone.utc)
            receipt.whatsapp_failure_reason = None
        else:
            receipt.whatsapp_status = "FAILED"
            reason = error_info.last_error if error_info else "unknown error"
            receipt.whatsapp_failure_reason = reason[:500]
            logger.warning(
                "Digital receipt WhatsApp send failed: order=%s, business=%s, reason=%s",
                getattr(order, "order_number", "?"), business.id, receipt.whatsapp_failure_reason,
            )

    return await _finish()
