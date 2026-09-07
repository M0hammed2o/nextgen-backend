"""
Receipt routes — the printable document for an order, in three forms.

    GET  /business/receipts/orders/{order_id}         -> structured JSON
    GET  /business/receipts/orders/{order_id}/text    -> plain monospaced text
    GET  /business/receipts/orders/{order_id}/escpos  -> raw ESC/POS bytes
    POST /business/receipts/orders/{order_id}/reprint -> audited reprint

PRINTING NEVER OWNS THE SALE
---------------------------
None of these endpoints can alter an order. Printing is strictly downstream
of a completed, paid sale, so a printer that is offline, out of paper or
unplugged cannot lose money that was already taken — the cashier simply
reprints. This separation is why the reprint endpoint exists at all.

Reprints are audited and carry a visible REPRINT banner, so a reprinted slip
is never mistaken for a second sale.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.receipts.document import build_receipt_document
from backend.app.receipts.escpos import PrinterCapabilities, render_escpos, render_text
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order
from shared.models.user import BusinessUser
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/business/receipts", tags=["receipts"])


async def _load(db: AsyncSession, business_id: uuid.UUID, order_id: uuid.UUID):
    """
    Load an order, its items and its business — all scoped to the caller's
    own business_id, which comes from the JWT and never from the client.
    """
    order = (
        await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id, Order.business_id == business_id)
        )
    ).scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))

    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    return order, business


async def _cashier_name(db: AsyncSession, business_id: uuid.UUID, user_id) -> str | None:
    if not user_id:
        return None
    row = (
        await db.execute(
            select(BusinessUser.name).where(
                BusinessUser.id == user_id, BusinessUser.business_id == business_id
            )
        )
    ).first()
    return row[0] if row else None


async def _document(
    db: AsyncSession,
    user: AuthUser,
    order_id: uuid.UUID,
    document_type: str,
    is_reprint: bool = False,
):
    order, business = await _load(db, user.business_id, order_id)
    cashier = await _cashier_name(db, user.business_id, getattr(order, "created_by_user_id", None))
    return build_receipt_document(
        order,
        business,
        document_type=document_type,
        cashier_name=cashier,
        is_reprint=is_reprint,
    )


@router.get("/orders/{order_id}")
async def get_receipt_document(
    order_id: uuid.UUID,
    document_type: str = Query("CUSTOMER_RECEIPT", pattern="^(CUSTOMER_RECEIPT|KITCHEN)$"),
    reprint: bool = Query(False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    The structured receipt, for the PWA to render and browser/kiosk-print.

    Content comes from the order's own snapshot fields, so reprinting an old
    receipt shows what was actually sold and charged rather than what the
    menu says today.
    """
    document = await _document(db, user, order_id, document_type, reprint)
    return document.to_dict()


@router.get("/orders/{order_id}/text", response_class=Response)
async def get_receipt_text(
    order_id: uuid.UUID,
    document_type: str = Query("CUSTOMER_RECEIPT", pattern="^(CUSTOMER_RECEIPT|KITCHEN)$"),
    reprint: bool = Query(False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Plain monospaced text at the tenant's paper width — useful for support and diagnosis."""
    document = await _document(db, user, order_id, document_type, reprint)
    return Response(content=render_text(document), media_type="text/plain; charset=utf-8")


@router.get("/orders/{order_id}/escpos", response_class=Response)
async def get_receipt_escpos(
    order_id: uuid.UUID,
    document_type: str = Query("CUSTOMER_RECEIPT", pattern="^(CUSTOMER_RECEIPT|KITCHEN)$"),
    reprint: bool = Query(False),
    open_drawer: bool = Query(False),
    supports_cut: bool = Query(True),
    supports_drawer: bool = Query(True),
    supports_qr: bool = Query(True),
    codepage: str = Query("cp437", max_length=16),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Raw ESC/POS bytes for the local print bridge to write to the device.

    Capabilities are query parameters rather than hardcoded per brand: a
    printer that cannot cut, kick a drawer or render a QR code simply does
    not receive those bytes, instead of printing them as stray characters.
    We do not know Ciao Pizza's model yet, and this endpoint does not need
    to — the bridge passes what its device config says.
    """
    document = await _document(db, user, order_id, document_type, reprint)
    capabilities = PrinterCapabilities(
        supports_cut=supports_cut,
        supports_drawer=supports_drawer,
        supports_qr=supports_qr,
        codepage=codepage,
    )
    payload = render_escpos(document, capabilities, open_drawer=open_drawer)
    return Response(content=payload, media_type="application/octet-stream")


@router.post("/orders/{order_id}/reprint")
async def reprint_receipt(
    order_id: uuid.UUID,
    document_type: str = Query("CUSTOMER_RECEIPT", pattern="^(CUSTOMER_RECEIPT|KITCHEN)$"),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Reprint a receipt, recording that it happened.

    Deliberately does not touch the order: a reprint is not a new sale, and
    an audit trail is what keeps a reprinted slip from being usable as
    evidence of a second one. The returned document carries a REPRINT banner.
    """
    document = await _document(db, user, order_id, document_type, is_reprint=True)

    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="receipt.reprinted",
            target_type="order",
            target_id=order_id,
            diff_json={"document_type": document_type, "actor_role": user.role},
        )
    )
    await db.commit()

    return document.to_dict()
