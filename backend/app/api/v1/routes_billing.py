"""
Business-plane billing routes — self-service SaaS subscription management.

Lets an OWNER start a subscription (Checkout), manage an existing one
(hosted Billing Portal, where the active provider supports one), or cancel
it, without contacting NextGen support. Goes entirely through
`backend/app/billing/registry.py::get_billing_provider()` — this file has
zero provider-specific logic, exactly like `routes_payments.py`'s
customer-facing Payment Provider Framework. The actual billing_status/
is_active sync on payment events happens via each provider's own webhook
(`stripe_webhooks.py`, `routes_billing_webhooks.py`) — these routes only
ever start or hand off to a provider-hosted flow.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import uuid
from datetime import datetime

from backend.app.billing.registry import get_billing_provider
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner
from backend.app.db.session import get_db
from shared.models.billing_invoice import BillingInvoice
from shared.models.business import Business

router = APIRouter(prefix="/business/billing", tags=["billing"])


class CheckoutSessionRequest(BaseModel):
    plan: str = Field(pattern=r"^(STARTER|GROWTH)$")


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


class PortalSessionResponse(BaseModel):
    portal_url: str | None
    supported: bool


class CancelSubscriptionResponse(BaseModel):
    success: bool
    status: str
    error: str | None = None


class BillingInvoiceResponse(BaseModel):
    id: uuid.UUID
    provider: str
    amount_cents: int
    status: str
    plan: str | None
    issued_at: datetime
    model_config = {"from_attributes": True}


class BillingStatusResponse(BaseModel):
    plan: str
    billing_status: str
    is_active: bool
    has_subscription: bool
    supports_self_service_portal: bool


async def _get_business(db: AsyncSession, user: AuthUser) -> Business:
    result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    return business


@router.get("/status", response_model=BillingStatusResponse)
async def get_billing_status(
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Current plan/billing status, for the dashboard's Billing settings page."""
    business = await _get_business(db, user)
    provider = get_billing_provider()
    has_subscription = bool(business.stripe_customer_id or business.payfast_subscription_token)
    return BillingStatusResponse(
        plan=business.plan,
        billing_status=business.billing_status,
        is_active=business.is_active,
        has_subscription=has_subscription,
        supports_self_service_portal=provider.supports_self_service_portal(),
    )


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    body: CheckoutSessionRequest,
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Start a new subscription via the active billing provider's hosted
    checkout. OWNER only — this is a business-level financial commitment,
    not a staff/manager action.
    """
    business = await _get_business(db, user)
    provider = get_billing_provider()
    url = await provider.create_checkout_session(db, business, body.plan)
    return CheckoutSessionResponse(checkout_url=url)


@router.post("/portal-session", response_model=PortalSessionResponse)
async def create_portal_session(
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Open the active provider's hosted self-service portal, if it has one.
    `supported=False` (with `portal_url=None`) when the active provider
    (e.g. PayFast) has no self-service equivalent — the dashboard should
    show "contact support to change your plan" instead of a broken link.
    """
    business = await _get_business(db, user)
    provider = get_billing_provider()
    if not provider.supports_self_service_portal():
        return PortalSessionResponse(portal_url=None, supported=False)
    url = await provider.create_portal_session(business)
    return PortalSessionResponse(portal_url=url, supported=True)


@router.post("/cancel", response_model=CancelSubscriptionResponse)
async def cancel_subscription(
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Cancel the business's active subscription with the active billing provider."""
    business = await _get_business(db, user)
    provider = get_billing_provider()
    result = await provider.cancel_subscription(db, business)
    if result.success:
        business.billing_status = "CANCELLED"
        await db.commit()
    return CancelSubscriptionResponse(success=result.success, status=result.status, error=result.error)


@router.get("/invoices", response_model=list[BillingInvoiceResponse])
async def list_invoices(
    user: AuthUser = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Invoice history for the Manager Dashboard. PDF rendering per invoice and
    automatic emailing are not built yet (no email/SMTP integration exists
    in this codebase at all — a real external-dependency gap, see TODO.md);
    this endpoint returns the stored record so the dashboard can at least
    list what was charged and when.
    """
    result = await db.execute(
        select(BillingInvoice)
        .where(BillingInvoice.business_id == user.business_id)
        .order_by(BillingInvoice.issued_at.desc())
    )
    return [BillingInvoiceResponse.model_validate(inv) for inv in result.scalars().all()]
