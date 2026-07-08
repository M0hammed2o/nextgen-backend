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

from backend.app.db.session import get_db
from fastapi import Depends
from shared.models.business import Business
from shared.models.order import Order, OrderEvent

logger = logging.getLogger("nextgen.payments.webhooks")

router = APIRouter(prefix="/payments/webhooks", tags=["payment-webhooks"])


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _mark_order_paid(
    db: AsyncSession,
    order_id_str: str,
    business_id: uuid.UUID,
    provider_name: str,
) -> None:
    """
    Mark an order PAID and send WhatsApp confirmation.

    Called by all three provider webhook handlers after signature verification.
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

    was_unpaid = order.payment_status in ("UNPAID", "PENDING")
    order.payment_status = "PAID"

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

    # Fire-and-forget WhatsApp notification
    if was_unpaid and order.payment_required and order.customer_id:
        try:
            from sqlalchemy import select as sa_select
            from shared.models.customer import Customer
            from backend.app.bot.whatsapp_sender import send_notification_message
            from backend.app.payments.messages import build_payment_confirmed_message

            cust = await db.execute(sa_select(Customer.wa_id).where(Customer.id == order.customer_id))
            wa_id = cust.scalar_one_or_none()

            biz = await db.execute(sa_select(Business).where(Business.id == business_id))
            biz_obj = biz.scalar_one_or_none()
            phone_number_id = biz_obj.whatsapp_phone_number_id if biz_obj else None

            if wa_id and phone_number_id:
                await send_notification_message(
                    phone_number_id=phone_number_id,
                    recipient_wa_id=wa_id,
                    text=build_payment_confirmed_message(order),
                )
        except Exception:
            logger.exception("Failed to send payment confirmation WhatsApp for order %s", order.order_number)


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
    webhook_secret = getattr(business, "payment_webhook_secret", None) or ""
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
        await _mark_order_paid(db, result["order_id"], business_id, "Yoco")

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
    passphrase = getattr(business, "payment_webhook_secret", None)
    # verify_signature pops "signature" from params dict
    params_copy = dict(params)
    if not PayFastProvider.verify_signature(params_copy, passphrase):
        logger.warning("PayFast ITN: invalid signature for business %s", business_id)
        raise HTTPException(status_code=400, detail="Invalid signature")

    provider = PayFastProvider()
    result = await provider.handle_webhook(params)

    if result.get("paid") and result.get("order_id"):
        await _mark_order_paid(db, result["order_id"], business_id, "PayFast")

    # PayFast expects a 200 OK with no body
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
    client_secret = getattr(business, "payment_api_key", None) or ""
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
        await _mark_order_paid(db, result["order_id"], business_id, "Stitch")

    return {"received": True}
