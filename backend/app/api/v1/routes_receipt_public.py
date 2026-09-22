"""
Public, unauthenticated digital receipt page.

    GET /v1/r/{token}  ->  self-contained mobile HTML

MOUNT POINT
-----------
Every other router in this codebase either requires a `require_*` RBAC
dependency, or (routes_webhook.py) is provider-signature-verified — there
is no existing precedent anywhere in main.py for an unauthenticated,
non-signature-verified route mounted outside the standard /v1 prefix. Per
this change's own task brief, the smallest deviation from convention is to
keep this under the existing /v1 prefix rather than inventing a new
un-prefixed mount point: `app.include_router(router, prefix=PREFIX)` in
main.py gives the final path `/v1/r/{token}`.

TOKEN IS THE ENTIRE LOOKUP KEY
--------------------------------
The route signature below takes ONLY `token: str` (plus the framework-
supplied `Request`/`db` dependencies, neither of which is client-suppliable
business data) — business_id/order_id/customer_id are never read from the
URL or query string at all, by construction. Every field shown on the page
comes from re-loading the order/business via the DigitalReceipt row the
token itself resolves to, scoped by that row's own business_id (never a
client-supplied one).

FAILURE SHAPE
--------------
Every failure mode — unknown token, malformed token, the order/business
somehow missing (should never happen given the FK CASCADE, but handled
anyway), or any unexpected error — returns the SAME generic 404 HTML page,
same status code, same headers. This is deliberate: distinguishing
"token doesn't exist" from "token exists but something else broke" would
be an enumeration/oracle risk for an unauthenticated endpoint. Nothing here
is ever logged with the raw token value, to avoid the token leaking into
log aggregation as if it were a low-sensitivity value — it is the entire
authorization for a real customer's order data, equivalent to a bearer
credential.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.config import get_settings
from backend.app.core.ratelimit import limiter
from backend.app.db.session import get_db
from backend.app.receipts.document import build_receipt_document
from backend.app.receipts.html_public import render_not_found_html, render_public_receipt_html
from shared.models.business import Business
from shared.models.digital_receipt import DigitalReceipt
from shared.models.order import Order

router = APIRouter(tags=["public-receipt"])
settings = get_settings()
logger = logging.getLogger("nextgen.receipts.public")

_NO_STORE_HEADERS = {"Cache-Control": "private, no-store"}


def _not_found() -> Response:
    return Response(
        content=render_not_found_html(),
        status_code=404,
        media_type="text/html; charset=utf-8",
        headers=_NO_STORE_HEADERS,
    )


@router.get("/r/{token}", response_class=Response)
@limiter.limit(f"{settings.RECEIPT_PUBLIC_RATE_LIMIT_PER_MINUTE}/minute")
async def get_public_receipt(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Render the public receipt page for `token`, or a generic 404 for any
    failure. See module docstring for the failure-shape and no-lookup-from-
    URL guarantees.
    """
    try:
        result = await db.execute(select(DigitalReceipt).where(DigitalReceipt.token == token))
        receipt = result.scalar_one_or_none()
        if receipt is None:
            return _not_found()

        order_result = await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == receipt.order_id, Order.business_id == receipt.business_id)
        )
        order = order_result.scalar_one_or_none()
        if order is None:
            return _not_found()

        business_result = await db.execute(select(Business).where(Business.id == receipt.business_id))
        business = business_result.scalar_one_or_none()
        if business is None:
            return _not_found()

        # Deliberately NOT gated on business.digital_receipts_enabled — a
        # receipt that was already generated/sent must remain viewable via
        # its own token regardless of the tenant's CURRENT entitlement
        # state (see DECISIONS.md and this change's report).
        document = build_receipt_document(order, business)
        html = render_public_receipt_html(document)
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers=_NO_STORE_HEADERS,
        )
    except SQLAlchemyError:
        logger.exception("Public receipt lookup failed (DB error)")
        return _not_found()
    except Exception:
        logger.exception("Public receipt lookup failed (unexpected error)")
        return _not_found()
