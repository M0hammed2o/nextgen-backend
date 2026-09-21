"""
Commercial invoicing (EFT/cash) + dashboard — Wave 0A sales-onboarding push.

Every route uses `get_business_commercial_scoped` (never `get_business_scoped`
— see admin_api/app/core/deps.py): a sales partner (ONBOARDER) keeps managing
trials/invoices/payments for their own clients even after that business's
onboarding is marked complete. SUPER_ADMIN is unrestricted, as everywhere else
on the admin plane.
"""

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.app.core.deps import get_business_commercial_scoped, require_onboarding_role
from admin_api.app.services.onboarding import compute_go_live_checks
from backend.app.billing.account_status import compute_account_status
from backend.app.billing.commercial_invoice_pdf import build_commercial_invoice_pdf
from backend.app.billing.commercial_invoices import (
    cancel_invoice,
    get_invoice_for_update,
    issue_invoice,
    mark_paid as _mark_paid,
    mark_sent as _mark_sent,
    recompute_invoice_totals,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import get_db
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.commercial_invoice import (
    UNPAID_ISSUED_STATUSES,
    CommercialInvoice,
    CommercialInvoiceLine,
)
from shared.enums import AuditScope

router = APIRouter(prefix="/admin/commercial", tags=["admin-commercial"])


# ── Price catalogue ──────────────────────────────────────────────────────────

class PriceCatalogueEntry(BaseModel):
    plan: str
    monthly_amount_cents: int
    monthly_amount_display: str


@router.get("/price-catalogue", response_model=list[PriceCatalogueEntry])
async def get_price_catalogue(user: AuthUser = Depends(require_onboarding_role)):
    """
    Read-only reference for building an invoice draft — PLAN monthly prices
    only. There is no catalogue for once-off setup fees or module add-ons;
    those line amounts are free entry on the invoice-line editor (no
    catalogue exists for them anywhere in this codebase today).
    """
    from backend.app.billing.payfast_billing import _PLAN_MONTHLY_AMOUNT_RANDS

    entries = []
    for plan, rand_str in _PLAN_MONTHLY_AMOUNT_RANDS.items():
        cents = int((Decimal(rand_str) * 100).to_integral_value())
        entries.append(PriceCatalogueEntry(plan=plan, monthly_amount_cents=cents, monthly_amount_display=f"R{rand_str}"))
    return entries


# ── Invoice schemas ───────────────────────────────────────────────────────────

class InvoiceLineIn(BaseModel):
    kind: str = Field(pattern=r"^(SETUP|SUBSCRIPTION|MODULE|OTHER)$")
    description: str = Field(min_length=1, max_length=512)
    quantity: int = Field(default=1, ge=1)
    unit_amount_cents: int = Field(ge=0)


class InvoiceLineOut(BaseModel):
    id: uuid.UUID
    sort_order: int
    kind: str
    description: str
    quantity: int
    unit_amount_cents: int
    line_total_cents: int
    model_config = {"from_attributes": True}


class InvoiceOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    invoice_number: str | None
    status: str
    invoice_date: date | None
    due_date: date | None
    currency: str
    subtotal_cents: int
    vat_cents: int
    vat_rate_basis_points: int
    total_cents: int
    payment_method: str | None
    paid_at: datetime | None
    payment_reference: str | None
    internal_note: str | None
    payment_note: str | None
    issued_at: datetime | None
    sent_at: datetime | None
    cancelled_at: datetime | None
    cancelled_reason: str | None
    created_at: datetime
    lines: list[InvoiceLineOut]
    model_config = {"from_attributes": True}


class InvoiceDraftCreate(BaseModel):
    business_id: uuid.UUID
    lines: list[InvoiceLineIn] = Field(default_factory=list)
    internal_note: str | None = Field(default=None, max_length=2000)


class InvoiceDraftUpdate(BaseModel):
    lines: list[InvoiceLineIn] | None = None
    internal_note: str | None = None


class MarkPaidRequest(BaseModel):
    payment_date: date
    payment_method: str = Field(pattern=r"^(EFT|CASH)$")
    reference: str | None = Field(default=None, max_length=255)
    internal_note: str | None = Field(default=None, max_length=2000)


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=512)


def _lines_from_body(lines_in: list[InvoiceLineIn]) -> list[CommercialInvoiceLine]:
    lines = []
    for i, line in enumerate(lines_in):
        lines.append(CommercialInvoiceLine(
            sort_order=i, kind=line.kind, description=line.description,
            quantity=line.quantity, unit_amount_cents=line.unit_amount_cents,
            line_total_cents=line.unit_amount_cents * line.quantity,
        ))
    return lines


async def _authorize_invoice(db: AsyncSession, invoice_id: uuid.UUID, user: AuthUser) -> Business:
    """Load the parent business and check commercial scope. Raises
    NotFoundError("CommercialInvoice", ...) either when the invoice truly
    doesn't exist OR when it belongs to a business out of this admin's
    reach — a cross-tenant probe on an invoice id learns nothing either way."""
    invoice = await db.get(CommercialInvoice, invoice_id)
    if invoice is None:
        raise NotFoundError("CommercialInvoice", str(invoice_id))
    try:
        return await get_business_commercial_scoped(invoice.business_id, user, db)
    except NotFoundError:
        raise NotFoundError("CommercialInvoice", str(invoice_id))


# ── Invoice CRUD ──────────────────────────────────────────────────────────────

@router.post("/invoices", response_model=InvoiceOut, status_code=201)
async def create_invoice_draft(
    body: InvoiceDraftCreate,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    await get_business_commercial_scoped(body.business_id, user, db)

    invoice = CommercialInvoice(
        business_id=body.business_id, status="DRAFT",
        internal_note=body.internal_note,
        created_by_admin_id=user.user_id,
    )
    invoice.lines = _lines_from_body(body.lines)
    recompute_invoice_totals(invoice)
    db.add(invoice)
    await db.flush()
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value, business_id=body.business_id, actor_user_id=user.user_id,
        action="admin.commercial_invoice.draft_created", target_type="commercial_invoice", target_id=invoice.id,
        diff_json={"line_count": len(invoice.lines)},
    ))
    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.get("/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    business_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    if business_id is not None:
        await get_business_commercial_scoped(business_id, user, db)
        query = select(CommercialInvoice).where(CommercialInvoice.business_id == business_id)
    else:
        query = select(CommercialInvoice)
        if user.role == AdminRole.ONBOARDER:
            query = query.join(Business, Business.id == CommercialInvoice.business_id).where(
                Business.onboarded_by_admin_id == user.user_id
            )
    if status:
        query = query.where(CommercialInvoice.status == status)
    query = query.order_by(CommercialInvoice.created_at.desc()).offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    invoices = result.scalars().unique().all()
    return [InvoiceOut.model_validate(inv) for inv in invoices]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    await _authorize_invoice(db, invoice_id, user)
    invoice = await db.get(CommercialInvoice, invoice_id)
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def update_invoice_draft(
    invoice_id: uuid.UUID,
    body: InvoiceDraftUpdate,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """Only a DRAFT may be edited — an issued invoice's lines/amounts are
    frozen forever; correct a mistake by cancelling and reissuing."""
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await get_invoice_for_update(db, invoice_id, business.id)
    if invoice.status != "DRAFT":
        raise AppError("INVALID_INVOICE_STATE", "Only a DRAFT invoice may be edited", 409)

    if body.lines is not None:
        invoice.lines = _lines_from_body(body.lines)
    if body.internal_note is not None:
        invoice.internal_note = body.internal_note
    recompute_invoice_totals(invoice)

    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
async def issue_invoice_route(
    invoice_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await get_invoice_for_update(db, invoice_id, business.id)
    await issue_invoice(db, invoice, business)
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value, business_id=business.id, actor_user_id=user.user_id,
        action="admin.commercial_invoice.issued", target_type="commercial_invoice", target_id=invoice.id,
        diff_json={"invoice_number": invoice.invoice_number, "total_cents": invoice.total_cents},
    ))
    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.post("/invoices/{invoice_id}/mark-sent", response_model=InvoiceOut)
async def mark_invoice_sent_route(
    invoice_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await get_invoice_for_update(db, invoice_id, business.id)
    _mark_sent(invoice)
    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.post("/invoices/{invoice_id}/mark-paid", response_model=InvoiceOut)
async def mark_invoice_paid_route(
    invoice_id: uuid.UUID,
    body: MarkPaidRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await get_invoice_for_update(db, invoice_id, business.id)
    await _mark_paid(
        db, invoice, business,
        payment_method=body.payment_method, payment_date=body.payment_date,
        reference=body.reference, note=body.internal_note, actor_admin_id=user.user_id,
    )
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value, business_id=business.id, actor_user_id=user.user_id,
        action="admin.commercial_invoice.marked_paid", target_type="commercial_invoice", target_id=invoice.id,
        diff_json={
            "payment_method": body.payment_method, "reference": body.reference,
            "total_cents": invoice.total_cents,
        },
    ))
    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceOut)
async def cancel_invoice_route(
    invoice_id: uuid.UUID,
    body: CancelRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await get_invoice_for_update(db, invoice_id, business.id)
    cancel_invoice(invoice, reason=body.reason, is_super_admin=(user.role == AdminRole.SUPER_ADMIN))
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value, business_id=business.id, actor_user_id=user.user_id,
        action="admin.commercial_invoice.cancelled", target_type="commercial_invoice", target_id=invoice.id,
        diff_json={"reason": body.reason},
    ))
    await db.commit()
    await db.refresh(invoice, attribute_names=["lines"])
    return InvoiceOut.model_validate(invoice)


@router.get(
    "/invoices/{invoice_id}/pdf",
    summary="Download a commercial invoice as a PDF",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_invoice_pdf(
    invoice_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    business = await _authorize_invoice(db, invoice_id, user)
    invoice = await db.get(CommercialInvoice, invoice_id)
    await db.refresh(invoice, attribute_names=["lines"])
    pdf_bytes = build_commercial_invoice_pdf(invoice, business)
    filename = f"{invoice.invoice_number or 'draft'}.pdf"
    return FastAPIResponse(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Dashboard & client list ──────────────────────────────────────────────────

class DashboardRow(BaseModel):
    business_id: uuid.UUID
    business_name: str
    account_status: str
    plan: str
    trial_ends_at: datetime | None
    days_remaining: int | None
    outstanding_cents: int
    onboarding_readiness: str
    onboarded_by: str | None = None


class CommercialDashboardResponse(BaseModel):
    counts: dict[str, int]
    trials_ending_soon: list[DashboardRow]
    payments_awaiting_confirmation: list[DashboardRow]
    overdue_clients: list[DashboardRow]
    recently_created_trials: list[DashboardRow]
    recently_converted: list[DashboardRow]
    expired_trials: list[DashboardRow]


async def _scoped_businesses(db: AsyncSession, user: AuthUser) -> list[Business]:
    query = select(Business)
    if user.role == AdminRole.ONBOARDER:
        query = query.where(Business.onboarded_by_admin_id == user.user_id)
    result = await db.execute(query)
    return list(result.scalars().all())


async def _open_invoices_by_business(db: AsyncSession, business_ids: list[uuid.UUID]) -> dict:
    grouped: dict[uuid.UUID, list[CommercialInvoice]] = defaultdict(list)
    if not business_ids:
        return grouped
    result = await db.execute(
        select(CommercialInvoice).where(
            CommercialInvoice.business_id.in_(business_ids),
            CommercialInvoice.status.in_(UNPAID_ISSUED_STATUSES),
        )
    )
    for inv in result.scalars().all():
        grouped[inv.business_id].append(inv)
    return grouped


@router.get("/dashboard", response_model=CommercialDashboardResponse)
async def get_commercial_dashboard(
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    The business list and the open-invoices list are each ONE query (no
    per-business N+1) — see `_scoped_businesses`/`_open_invoices_by_business`.
    `onboarding_readiness` ("x/y") is the one deliberate exception: it reuses
    `compute_go_live_checks`, which issues several small queries PER
    business, and is only computed for rows that actually land in one of the
    bounded output buckets below (never for the full scoped business set) —
    an accepted, documented trade-off for the realistic per-onboarder client
    volumes this "smallest production-safe version" targets, not a silent
    N+1 on the whole tenant base.
    """
    businesses = await _scoped_businesses(db, user)
    business_ids = [b.id for b in businesses]
    invoices_by_business = await _open_invoices_by_business(db, business_ids)

    admin_names: dict[uuid.UUID, str] = {}
    if user.role == AdminRole.SUPER_ADMIN:
        onboarder_ids = {b.onboarded_by_admin_id for b in businesses if b.onboarded_by_admin_id}
        if onboarder_ids:
            admins = (await db.execute(select(AdminUser).where(AdminUser.id.in_(onboarder_ids)))).scalars().all()
            admin_names = {a.id: a.display_name for a in admins}

    now = datetime.now(timezone.utc)
    counts = {"trial": 0, "active": 0, "payment_due": 0, "overdue": 0, "expired": 0, "suspended": 0}
    buckets: dict[str, list[DashboardRow]] = defaultdict(list)

    for biz in businesses:
        open_invs = invoices_by_business.get(biz.id, [])
        acct = compute_account_status(biz, open_invs, now)
        counts_key = acct.status.lower()
        if counts_key in counts:
            counts[counts_key] += 1

        bucket_names = []
        if any(a.startswith("TRIAL_ENDS_") for a in acct.alerts):
            bucket_names.append("trials_ending_soon")
        if acct.status == "PAYMENT_DUE":
            bucket_names.append("payments_awaiting_confirmation")
        if acct.status == "OVERDUE":
            bucket_names.append("overdue_clients")
        if biz.trial_started_at and (now - biz.trial_started_at) <= timedelta(days=14):
            bucket_names.append("recently_created_trials")
        if biz.converted_at and (now - biz.converted_at) <= timedelta(days=30):
            bucket_names.append("recently_converted")
        if acct.status == "EXPIRED":
            bucket_names.append("expired_trials")

        if not bucket_names:
            continue

        go_live = await compute_go_live_checks(db, biz)
        row = DashboardRow(
            business_id=biz.id, business_name=biz.name, account_status=acct.status,
            plan=biz.plan, trial_ends_at=biz.trial_ends_at, days_remaining=acct.trial_days_remaining,
            outstanding_cents=sum(inv.total_cents for inv in open_invs),
            onboarding_readiness=f"{go_live.readiness.passed}/{go_live.readiness.total}",
            onboarded_by=(admin_names.get(biz.onboarded_by_admin_id) if user.role == AdminRole.SUPER_ADMIN else None),
        )
        for bucket in bucket_names:
            buckets[bucket].append(row)

    buckets["trials_ending_soon"].sort(key=lambda r: (r.days_remaining if r.days_remaining is not None else 999))

    return CommercialDashboardResponse(
        counts=counts,
        trials_ending_soon=buckets["trials_ending_soon"],
        payments_awaiting_confirmation=buckets["payments_awaiting_confirmation"],
        overdue_clients=buckets["overdue_clients"],
        recently_created_trials=buckets["recently_created_trials"],
        recently_converted=buckets["recently_converted"],
        expired_trials=buckets["expired_trials"],
    )


class ClientRow(BaseModel):
    business_id: uuid.UUID
    business_name: str
    account_status: str
    plan: str
    trial_ends_at: datetime | None
    days_remaining: int | None
    outstanding_cents: int
    onboarded_by: str | None = None


@router.get("/clients", response_model=list[ClientRow])
async def list_commercial_clients(
    status: str | None = Query(default=None, description="Filter by derived account_status"),
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Flat client list — every scoped business with its derived commercial
    status. Deliberately does NOT compute onboarding_readiness (unlike the
    dashboard's bounded buckets) to keep this a genuine single-query-per-list
    endpoint even for a large client base.
    """
    businesses = await _scoped_businesses(db, user)
    business_ids = [b.id for b in businesses]
    invoices_by_business = await _open_invoices_by_business(db, business_ids)

    admin_names: dict[uuid.UUID, str] = {}
    if user.role == AdminRole.SUPER_ADMIN:
        onboarder_ids = {b.onboarded_by_admin_id for b in businesses if b.onboarded_by_admin_id}
        if onboarder_ids:
            admins = (await db.execute(select(AdminUser).where(AdminUser.id.in_(onboarder_ids)))).scalars().all()
            admin_names = {a.id: a.display_name for a in admins}

    now = datetime.now(timezone.utc)
    rows = []
    for biz in businesses:
        open_invs = invoices_by_business.get(biz.id, [])
        acct = compute_account_status(biz, open_invs, now)
        if status and acct.status != status:
            continue
        rows.append(ClientRow(
            business_id=biz.id, business_name=biz.name, account_status=acct.status,
            plan=biz.plan, trial_ends_at=biz.trial_ends_at, days_remaining=acct.trial_days_remaining,
            outstanding_cents=sum(inv.total_cents for inv in open_invs),
            onboarded_by=(admin_names.get(biz.onboarded_by_admin_id) if user.role == AdminRole.SUPER_ADMIN else None),
        ))
    return rows
