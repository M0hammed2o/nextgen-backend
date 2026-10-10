"""
Payment provider webhook routes.

These endpoints are called by Yoco / PayFast / Stitch — NOT by the frontend.
They do NOT require JWT authentication; instead each route verifies the
provider's own signature before processing.

Route pattern:  POST /v1/payments/webhooks/{provider}/{business_id}

The {business_id} in the path is how the system knows which business the
payment belongs to without relying on payload content before verification.
Business owners configure this full URL in their payment provider dashboard.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.crypto import decrypt_credential
from backend.app.db.session import get_db
from fastapi import Depends
from shared.models.business import Business
from shared.models.order import Order, OrderEvent
from shared.models.pos_payment import POSPaymentLinkIntent

logger = logging.getLogger("nextgen.payments.webhooks")

router = APIRouter(prefix="/payments/webhooks", tags=["payment-webhooks"])


# ── Shared helper ─────────────────────────────────────────────────────────────

def _amount_currency_ok(
    order: Order, amount_cents: int | None, currency: str | None, provider_name: str,
) -> bool:
    """
    Verify a provider-reported paid amount/currency against the order before
    it's trusted. No partial-payment flow exists anywhere in this platform —
    any mismatch means the payment must NOT be marked PAID.

    Both amount_cents and currency are optional: not every provider's
    webhook payload actually carries them (see the KNOWN GAP comments in
    backend/app/payments/ikhoka.py and stitch.py) — when a provider
    genuinely doesn't send one, that check is skipped rather than invented,
    which is a real, disclosed limitation, not silently ignored.
    """
    if amount_cents is not None:
        if int(amount_cents) != int(order.total_cents):
            logger.warning(
                "%s webhook: AMOUNT MISMATCH for order %s — expected %d cents, provider reported %d cents. "
                "NOT marking paid.",
                provider_name, order.order_number, order.total_cents, amount_cents,
            )
            return False
    if currency:
        expected_currency = (order.currency or "ZAR").upper()
        if str(currency).upper() != expected_currency:
            logger.warning(
                "%s webhook: CURRENCY MISMATCH for order %s — expected %s, provider reported %s. "
                "NOT marking paid.",
                provider_name, order.order_number, expected_currency, str(currency).upper(),
            )
            return False
    return True


async def _mark_order_paid(
    db: AsyncSession,
    order_id_str: str,
    business_id: uuid.UUID,
    provider_name: str,
    provider_reference: str | None = None,
    amount_cents: int | None = None,
    currency: str | None = None,
) -> None:
    """
    Mark an order PAID and send WhatsApp confirmation.

    Called by all four provider webhook handlers after signature verification.
    provider_reference (the provider's own charge/transaction id, e.g. Yoco's
    "chr_xxx" or iKhoka's paylinkID) is persisted on Order.payment_reference
    so a later refund_payment() call has something real to target — without
    this, a refund could only ever act on our own order UUID, which none of
    these providers' refund APIs accept.

    amount_cents/currency (when the provider's payload actually carries
    them — see _amount_currency_ok) are verified against the order BEFORE
    it is marked paid, so a provider confirming payment for the wrong amount
    (e.g. a stale/reused payment link after the order total changed) cannot
    silently mark the order paid.
    """
    try:
        parsed_id = uuid.UUID(str(order_id_str))
    except (ValueError, AttributeError):
        logger.warning("%s webhook: invalid order_id %r", provider_name, order_id_str)
        return

    result = await db.execute(
        select(Order).where(Order.id == parsed_id, Order.business_id == business_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        logger.warning("%s webhook: order %s not found for business %s", provider_name, parsed_id, business_id)
        return

    if order.payment_status == "PAID":
        logger.info("%s webhook: order %s already PAID — skipping", provider_name, parsed_id)
        return

    if not _amount_currency_ok(order, amount_cents, currency, provider_name):
        return

    was_unpaid = order.payment_status in ("UNPAID", "PENDING")
    order.payment_status = "PAID"
    if provider_reference and not order.payment_reference:
        order.payment_reference = provider_reference

    from datetime import datetime, timezone
    order.paid_at = datetime.now(timezone.utc)

    db.add(OrderEvent(
        order_id=order.id,
        business_id=order.business_id,
        old_status=order.status,
        new_status=order.status,
        reason=f"Payment confirmed via {provider_name} webhook",
    ))
    await db.commit()
    await db.refresh(order)

    logger.info("%s webhook: order %s marked PAID", provider_name, order.order_number)

    # Award loyalty points if this business has loyalty enabled
    from backend.app.loyalty.engine import award_loyalty_points_if_enabled
    await award_loyalty_points_if_enabled(db, business_id, order)

    # Fire-and-forget WhatsApp notification + digital receipt (template).
    #
    # Both are sent here so the customer gets instant feedback (short
    # confirmation text, unchanged) AND a durable, template-based digital
    # receipt pointing at the public receipt page — see
    # backend/app/receipts/whatsapp_receipt.py::send_digital_receipt(). This
    # single call site is shared by all four payment-provider webhook
    # handlers (Yoco/PayFast/Stitch/iKhoka), so wiring the receipt in here
    # covers every provider without duplicating the send at each route.
    #
    # Idempotency (two layers): the `order.payment_status == "PAID"` early-
    # return above already prevents this whole block from running twice for
    # the same order on a duplicate webhook delivery; send_digital_receipt
    # itself is separately idempotent per order_id (get-or-create + already-
    # SENT short-circuit) regardless.
    if was_unpaid and order.payment_required and order.customer_id:
        try:
            from sqlalchemy import select as sa_select
            from shared.models.customer import Customer
            from backend.app.bot.whatsapp_sender import send_notification_message
            from backend.app.payments.messages import build_payment_confirmed_message
            from backend.app.receipts.whatsapp_receipt import send_digital_receipt

            cust = await db.execute(sa_select(Customer).where(Customer.id == order.customer_id))
            cust_obj = cust.scalar_one_or_none()
            wa_id = cust_obj.wa_id if cust_obj else None

            biz = await db.execute(sa_select(Business).where(Business.id == business_id))
            biz_obj = biz.scalar_one_or_none()
            phone_number_id = biz_obj.whatsapp_phone_number_id if biz_obj else None

            if wa_id and phone_number_id:
                await send_notification_message(
                    phone_number_id=phone_number_id,
                    recipient_wa_id=wa_id,
                    text=build_payment_confirmed_message(order),
                    db=db, business=biz_obj,
                    order_id=order.id, business_id=business_id,
                    order_status=order.status,
                )

            if biz_obj is not None:
                await send_digital_receipt(db, order, biz_obj, channels={"WHATSAPP"}, customer=cust_obj)
        except Exception:
            logger.exception("Failed to send payment confirmation WhatsApp for order %s", order.order_number)


_POS_INTENT_PREFIX = "pos_intent:"


async def _mark_intent_paid(
    db: AsyncSession,
    intent_ref: str,
    business_id: uuid.UUID,
    provider_name: str,
    *,
    provider_reference: str | None,
    amount_cents: int | None,
    currency: str | None,
) -> None:
    """
    Mark a POS Pay-by-Link intent (`POSPaymentLinkIntent`) PAID.

    Mirrors `_mark_order_paid`'s structure/discipline (never raise on a bad
    webhook delivery, tenant-scope every lookup, verify amount/currency
    before trusting the provider's say-so) but is scoped to exactly the
    intent row — no `Order` exists yet for a POS intent at this point, so
    this function never touches one, and it never sends any WhatsApp
    message (the Staff app learns PAID via its own polling of
    `GET /v1/business/pos/payment-intents/{id}`).

    intent_ref is the raw string after the webhook routing branch strips
    the "pos_intent:" prefix — parsed to a UUID here (not by the caller) so
    a malformed id is this function's problem to log-and-return on, never
    the caller's to raise on.
    """
    try:
        parsed_id = uuid.UUID(str(intent_ref))
    except (ValueError, AttributeError):
        logger.warning("%s webhook: invalid POS payment intent id %r", provider_name, intent_ref)
        return

    result = await db.execute(
        select(POSPaymentLinkIntent)
        .where(
            POSPaymentLinkIntent.id == parsed_id,
            POSPaymentLinkIntent.business_id == business_id,
        )
        .with_for_update()
    )
    intent = result.scalar_one_or_none()
    if not intent:
        logger.warning(
            "%s webhook: POS payment intent %s not found for business %s",
            provider_name, parsed_id, business_id,
        )
        return

    if intent.status in ("PAID", "CONSUMED"):
        # Duplicate webhook delivery (providers retry). First-write-wins:
        # a matching reference is a benign idempotent no-op; a mismatching
        # one is logged as a potential anomaly but still never re-processed.
        if provider_reference and intent.provider_reference == provider_reference:
            logger.debug(
                "%s webhook: POS payment intent %s already %s with matching "
                "reference — idempotent no-op",
                provider_name, parsed_id, intent.status,
            )
        else:
            logger.warning(
                "%s webhook: POS payment intent %s already %s with reference %r — "
                "incoming reference %r does not match; NOT re-processing "
                "(first-write-wins)",
                provider_name, parsed_id, intent.status, intent.provider_reference, provider_reference,
            )
        return

    if intent.status != "PENDING":
        # FAILED/EXPIRED/CANCELLED — never resurrect a dead intent.
        logger.warning(
            "%s webhook: POS payment intent %s is %s (not PENDING) — not transitioning",
            provider_name, parsed_id, intent.status,
        )
        return

    amount_ok = amount_cents is not None and int(amount_cents) == int(intent.amount_cents)
    currency_ok = currency is None or str(currency).upper() == (intent.currency or "ZAR").upper()
    if not amount_ok or not currency_ok:
        logger.warning(
            "%s webhook: POS payment intent %s AMOUNT/CURRENCY MISMATCH — expected "
            "%d cents %s, provider reported %r cents %r. NOT marking paid.",
            provider_name, parsed_id, intent.amount_cents, intent.currency, amount_cents, currency,
        )
        return

    from datetime import datetime, timezone
    intent.status = "PAID"
    intent.provider_reference = provider_reference
    intent.paid_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info("%s webhook: POS payment intent %s marked PAID", provider_name, parsed_id)


async def _load_business(db: AsyncSession, business_id: uuid.UUID) -> Business | None:
    result = await db.execute(select(Business).where(Business.id == business_id))
    return result.scalar_one_or_none()


# ── Yoco ──────────────────────────────────────────────────────────────────────

@router.post("/yoco/{business_id}", include_in_schema=False)
async def yoco_webhook(
    business_id: uuid.UUID = Path(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive Yoco payment webhook events.

    Yoco calls this URL when a checkout is completed.
    Configure in Yoco Dashboard → Webhooks:
        URL: https://<api-domain>/v1/payments/webhooks/yoco/<business_id>
    """
    raw_body = await request.body()
    sig = request.headers.get("X-Yoco-Signature", "")

    business = await _load_business(db, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    from backend.app.payments.yoco import YocoProvider
    webhook_secret = decrypt_credential(getattr(business, "payment_webhook_secret", None)) or ""
    if not YocoProvider.verify_signature(raw_body, sig, webhook_secret):
        logger.warning("Yoco webhook: invalid signature for business %s", business_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    provider = YocoProvider()
    result = await provider.handle_webhook(payload)

    if result.get("paid") and result.get("order_id"):
        order_ref = str(result["order_id"])
        if order_ref.startswith(_POS_INTENT_PREFIX):
            await _mark_intent_paid(
                db, order_ref[len(_POS_INTENT_PREFIX):], business_id, "Yoco",
                provider_reference=result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )
        else:
            await _mark_order_paid(
                db, result["order_id"], business_id, "Yoco", result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )

    return {"received": True}


# ── PayFast ───────────────────────────────────────────────────────────────────

@router.post("/payfast/{business_id}", include_in_schema=False)
async def payfast_webhook(
    business_id: uuid.UUID = Path(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive PayFast ITN (Instant Transaction Notification).

    PayFast POSTs form data to this URL after payment.
    The notify_url is embedded in the payment link automatically.
    """
    raw_body = await request.body()

    # PayFast sends application/x-www-form-urlencoded
    import urllib.parse
    try:
        params = dict(urllib.parse.parse_qsl(raw_body.decode("utf-8")))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid form data")

    business = await _load_business(db, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    from backend.app.payments.payfast import PayFastProvider
    passphrase = decrypt_credential(getattr(business, "payment_webhook_secret", None))
    # verify_signature pops "signature" from params dict
    params_copy = dict(params)
    if not PayFastProvider.verify_signature(params_copy, passphrase):
        logger.warning("PayFast ITN: invalid signature for business %s", business_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    provider = PayFastProvider()
    result = await provider.handle_webhook(params)

    if result.get("paid") and result.get("order_id"):
        order_ref = str(result["order_id"])
        if order_ref.startswith(_POS_INTENT_PREFIX):
            await _mark_intent_paid(
                db, order_ref[len(_POS_INTENT_PREFIX):], business_id, "PayFast",
                provider_reference=result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )
        else:
            await _mark_order_paid(
                db, result["order_id"], business_id, "PayFast", result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )

    # PayFast expects a 200 OK with no body
    return {"received": True}


# ── iKhoka ───────────────────────────────────────────────────────────────────

@router.post("/ikhoka/{business_id}", include_in_schema=False)
async def ikhoka_webhook(
    business_id: uuid.UUID = Path(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive iKhoka payment callback.

    iKhoka calls the callbackUrl embedded in each payment link at creation time.
    No separate dashboard webhook configuration is required.
    The URL pattern is: https://<api-domain>/v1/payments/webhooks/ikhoka/<business_id>
    """
    raw_body = await request.body()
    ik_sign = request.headers.get("ik-sign", "")

    business = await _load_business(db, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    from backend.app.payments.ikhoka import IKhokaProvider
    # iKhoka uses the Application Secret (payment_api_secret) for webhook signing —
    # the same secret used to sign outbound API requests.
    app_secret = decrypt_credential(getattr(business, "payment_api_secret", None)) or ""
    callback_path = f"/v1/payments/webhooks/ikhoka/{business_id}"

    if not IKhokaProvider.verify_signature(raw_body, ik_sign, app_secret, callback_path):
        logger.warning("iKhoka webhook: invalid signature for business %s", business_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    provider = IKhokaProvider()
    result = await provider.handle_webhook(payload)

    if result.get("paid") and result.get("order_id"):
        order_ref = str(result["order_id"])
        if order_ref.startswith(_POS_INTENT_PREFIX):
            # iKhoka is excluded from POS Pay-by-Link (see module docstring
            # KNOWN GAP below) so this branch should be unreachable in
            # practice — kept for structural consistency with the other
            # three handlers and defence-in-depth, not because iKhoka can
            # actually originate a "pos_intent:" reference today.
            await _mark_intent_paid(
                db, order_ref[len(_POS_INTENT_PREFIX):], business_id, "iKhoka",
                provider_reference=result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )
        else:
            # KNOWN GAP: iKhoka's callback payload carries no amount/currency —
            # see backend/app/payments/ikhoka.py's handle_webhook docstring.
            # amount_cents/currency will be None here; _mark_order_paid skips
            # that check rather than inventing a value to compare against.
            await _mark_order_paid(
                db, result["order_id"], business_id, "iKhoka", result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )

    return {"received": True}


# ── Stitch ────────────────────────────────────────────────────────────────────

@router.post("/stitch/{business_id}", include_in_schema=False)
async def stitch_webhook(
    business_id: uuid.UUID = Path(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive Stitch payment webhook events.

    Configure in Stitch Dashboard → Webhooks:
        URL: https://<api-domain>/v1/payments/webhooks/stitch/<business_id>
    """
    raw_body = await request.body()
    sig = request.headers.get("X-Stitch-Signature", "")

    business = await _load_business(db, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    from backend.app.payments.stitch import StitchProvider
    # Stitch signs with client_secret (same as payment_api_key)
    client_secret = decrypt_credential(getattr(business, "payment_api_key", None)) or ""
    if not StitchProvider.verify_signature(raw_body, sig, client_secret):
        logger.warning("Stitch webhook: invalid signature for business %s", business_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    import json
    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    provider = StitchProvider()
    result = await provider.handle_webhook(payload)

    if result.get("paid") and result.get("order_id"):
        order_ref = str(result["order_id"])
        if order_ref.startswith(_POS_INTENT_PREFIX):
            # Stitch is excluded from POS Pay-by-Link (see module docstring
            # KNOWN GAP below) so this branch should be unreachable in
            # practice — kept for structural consistency with the other
            # three handlers and defence-in-depth, not because Stitch can
            # actually originate a "pos_intent:" reference today.
            await _mark_intent_paid(
                db, order_ref[len(_POS_INTENT_PREFIX):], business_id, "Stitch",
                provider_reference=result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )
        else:
            # KNOWN GAP: Stitch's payment_initiation_request.complete payload as
            # implemented here carries no amount — see
            # backend/app/payments/stitch.py's handle_webhook docstring.
            await _mark_order_paid(
                db, result["order_id"], business_id, "Stitch", result.get("reference"),
                amount_cents=result.get("amount_cents"), currency=result.get("currency"),
            )

    return {"received": True}
