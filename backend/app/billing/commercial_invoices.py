"""
EFT/cash commercial-invoice lifecycle: draft -> issue -> mark-paid/cancel.

Distinct from `backend/app/billing/lifecycle.py`, which handles Stripe/
PayFast SUBSCRIPTION payment events. This module handles NextGen manually
invoicing a restaurant client for setup/subscription/module fees, paid by
EFT or cash and recorded by a sales partner — but it REUSES `lifecycle.py`'s
`mark_invoice_paid()` the moment a commercial invoice is marked paid, so
"pay by card via Stripe" and "pay by EFT, confirmed by a human" both flow
through the exact same business-reactivation/conversion logic and both leave
a row in the one shared `billing_invoices` payment-history table.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.billing.lifecycle import mark_invoice_paid as _lifecycle_mark_invoice_paid
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from shared.models.business import Business
from shared.models.commercial_invoice import (
    CommercialInvoice,
    CommercialInvoiceLine,
    UNPAID_ISSUED_STATUSES,
)

# Config names that MUST be set before any invoice can be issued — checked
# every time, not just at boot, since a fresh deploy with these unset must
# never let a draft become a real numbered document with placeholder/blank
# supplier or banking details. NEXTGEN_TRADING_NAME/CONTACT_PHONE/
# REGISTRATION_NUMBER/VAT_NUMBER are deliberately optional (a real business
# can lack a trading name, and VAT fields are conditionally required only
# when NEXTGEN_VAT_REGISTERED is true, checked separately below).
REQUIRED_INVOICING_CONFIG_FIELDS = [
    "NEXTGEN_LEGAL_NAME",
    "NEXTGEN_ADDRESS",
    "NEXTGEN_CONTACT_EMAIL",
    "NEXTGEN_BANK_NAME",
    "NEXTGEN_BANK_ACCOUNT_NAME",
    "NEXTGEN_BANK_ACCOUNT_NUMBER",
    "NEXTGEN_BANK_BRANCH_CODE",
]


def get_missing_invoicing_config() -> list[str]:
    """Names (not values — never leak a partially-set secret) of every
    required NEXTGEN_* config var that is unset, plus NEXTGEN_VAT_NUMBER if
    NextGen is marked VAT-registered but has no VAT number configured."""
    settings = get_settings()
    missing = [name for name in REQUIRED_INVOICING_CONFIG_FIELDS if not getattr(settings, name)]
    if settings.NEXTGEN_VAT_REGISTERED and not settings.NEXTGEN_VAT_NUMBER:
        missing.append("NEXTGEN_VAT_NUMBER")
    return missing


def recompute_invoice_totals(invoice: CommercialInvoice) -> None:
    """
    Recompute subtotal/VAT/total from `invoice.lines`. Line prices are
    EXCLUSIVE of VAT (the normal B2B convention) — VAT is added on top, not
    backed out of the line prices, unlike the tenant-facing menu-price VAT
    engine (`shared/pricing/engine.py::apply_vat_to_total`, which defaults
    to VAT-INCLUSIVE for consumer menu prices). Must be called after ANY
    line add/edit/remove, and again right before issuing.
    """
    from shared.pricing.engine import calculate_vat_cents

    settings = get_settings()
    subtotal = sum(line.line_total_cents for line in invoice.lines)

    if settings.NEXTGEN_VAT_REGISTERED and settings.NEXTGEN_VAT_RATE_BPS > 0:
        vat_cents = calculate_vat_cents(subtotal, settings.NEXTGEN_VAT_RATE_BPS, vat_inclusive=False)
        invoice.vat_rate_basis_points = settings.NEXTGEN_VAT_RATE_BPS
    else:
        vat_cents = 0
        invoice.vat_rate_basis_points = 0

    invoice.subtotal_cents = subtotal
    invoice.vat_cents = vat_cents
    invoice.total_cents = subtotal + vat_cents


async def get_invoice_for_update(
    db: AsyncSession, invoice_id, business_id=None,
) -> CommercialInvoice:
    """
    Row-locked fetch (`SELECT ... FOR UPDATE`) for any mutating action
    (issue/mark-paid/cancel) — the concurrency guard against two admins (or
    a retried client request) racing the same invoice into two different
    terminal states, and specifically against two concurrent `issue` calls
    both trying to assign a number (the sequence itself is race-free
    regardless, but the invoice row's own state transition is not without
    this lock).
    """
    query = select(CommercialInvoice).where(CommercialInvoice.id == invoice_id)
    if business_id is not None:
        query = query.where(CommercialInvoice.business_id == business_id)
    query = query.with_for_update()
    result = await db.execute(query)
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise NotFoundError("CommercialInvoice", str(invoice_id))
    # with_for_update() alone doesn't guarantee lines are loaded under the
    # same lock ordering concerns here (lines are only read, never raced),
    # so an explicit refresh isn't required — selectin-loaded relationship
    # is fetched by a separate SELECT regardless of FOR UPDATE.
    await db.refresh(invoice, attribute_names=["lines"])
    return invoice


async def issue_invoice(
    db: AsyncSession, invoice: CommercialInvoice, business: Business, *, now: datetime | None = None,
) -> None:
    """
    DRAFT -> AWAITING_PAYMENT. Assigns the next sequential invoice number
    from the Postgres SEQUENCE `commercial_invoice_number_seq` — sequences
    are non-transactional in Postgres, so two concurrent `issue` calls are
    guaranteed distinct `nextval()` results with no explicit locking needed
    for the number itself (the row lock above still guards the rest of this
    invoice's own state transition). Freezes lines (nothing here mutates
    them further) and snapshots both the client's and NextGen's own details
    so a later change to either can never alter an already-issued document.
    """
    if invoice.status != "DRAFT":
        raise AppError(
            "INVALID_INVOICE_STATE",
            f"Only a DRAFT invoice can be issued (current status: {invoice.status})",
            409,
        )
    if not invoice.lines:
        raise AppError("INVOICE_HAS_NO_LINES", "Cannot issue an invoice with no line items", 422)

    missing = get_missing_invoicing_config()
    if missing:
        raise AppError(
            "INVOICING_NOT_CONFIGURED",
            "NextGen's invoicing details are not fully configured: " + ", ".join(missing),
            409,
            details={"missing": missing},
        )

    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    recompute_invoice_totals(invoice)

    seq_result = await db.execute(text("SELECT nextval('commercial_invoice_number_seq')"))
    seq_value = int(seq_result.scalar_one())
    invoice.invoice_number = f"NG-INV-{now.year}-{seq_value:05d}"

    invoice.invoice_date = now.date()
    invoice.due_date = (now + timedelta(days=settings.NEXTGEN_INVOICE_PAYMENT_TERMS_DAYS)).date()
    invoice.issued_at = now
    invoice.status = "AWAITING_PAYMENT"

    invoice.customer_snapshot = {
        "name": business.name,
        "trading_name": business.name,
        "address": business.address,
        "phone": business.phone,
        "vat_registered": business.vat_registered,
        "vat_number": business.vat_number,
    }
    invoice.supplier_snapshot = {
        "legal_name": settings.NEXTGEN_LEGAL_NAME,
        "trading_name": settings.NEXTGEN_TRADING_NAME,
        "registration_number": settings.NEXTGEN_REGISTRATION_NUMBER,
        "address": settings.NEXTGEN_ADDRESS,
        "contact_email": settings.NEXTGEN_CONTACT_EMAIL,
        "contact_phone": settings.NEXTGEN_CONTACT_PHONE,
        "vat_registered": settings.NEXTGEN_VAT_REGISTERED,
        "vat_number": settings.NEXTGEN_VAT_NUMBER,
        "bank_name": settings.NEXTGEN_BANK_NAME,
        "bank_account_name": settings.NEXTGEN_BANK_ACCOUNT_NAME,
        "bank_account_number": settings.NEXTGEN_BANK_ACCOUNT_NUMBER,
        "bank_branch_code": settings.NEXTGEN_BANK_BRANCH_CODE,
        "bank_account_type": settings.NEXTGEN_BANK_ACCOUNT_TYPE,
        "payment_terms_days": settings.NEXTGEN_INVOICE_PAYMENT_TERMS_DAYS,
    }


def mark_sent(invoice: CommercialInvoice, *, now: datetime | None = None) -> None:
    """AWAITING_PAYMENT -> SENT. Optional step — both statuses count as
    'issued and unpaid' everywhere else in the system."""
    if invoice.status not in ("AWAITING_PAYMENT", "SENT", "OVERDUE"):
        raise AppError(
            "INVALID_INVOICE_STATE",
            f"Cannot mark a {invoice.status} invoice as sent",
            409,
        )
    invoice.status = "SENT"
    invoice.sent_at = now or datetime.now(timezone.utc)


async def mark_paid(
    db: AsyncSession,
    invoice: CommercialInvoice,
    business: Business,
    *,
    payment_method: str,
    payment_date: date,
    reference: str | None,
    note: str | None,
    actor_admin_id,
    now: datetime | None = None,
) -> None:
    """
    Idempotent: an already-PAID invoice raises 409 rather than double-
    converting the business or double-writing a `billing_invoices` row.
    Reuses `lifecycle.mark_invoice_paid` for the actual
    reactivate/convert-the-business side effect so EFT/cash payments behave
    identically to a Stripe/PayFast webhook payment.
    """
    if invoice.status == "PAID":
        raise AppError("ALREADY_PAID", "This invoice has already been marked paid", 409)
    if invoice.status not in UNPAID_ISSUED_STATUSES:
        raise AppError(
            "INVALID_INVOICE_STATE",
            f"Cannot mark a {invoice.status} invoice as paid — it must be issued first",
            409,
        )
    if payment_method not in ("EFT", "CASH"):
        raise AppError("INVALID_PAYMENT_METHOD", "payment_method must be EFT or CASH", 422)
    if payment_method == "EFT" and not reference:
        raise AppError("REFERENCE_REQUIRED", "A payment reference is required for an EFT payment", 422)

    now = now or datetime.now(timezone.utc)
    invoice.status = "PAID"
    invoice.paid_at = now
    invoice.payment_method = payment_method
    invoice.payment_reference = reference
    invoice.payment_note = note
    invoice.paid_recorded_by_admin_id = actor_admin_id

    _lifecycle_mark_invoice_paid(
        business,
        db=db,
        amount_cents=invoice.total_cents,
        provider=payment_method,
        provider_reference=reference or invoice.invoice_number,
    )
    if business.converted_at is None:
        business.converted_at = now


def cancel_invoice(
    invoice: CommercialInvoice, *, reason: str, is_super_admin: bool, now: datetime | None = None,
) -> None:
    """
    DRAFT or issued -> CANCELLED. A PAID invoice can never be cancelled —
    correct a real mistake by cancelling + reissuing a NEW draft, never by
    editing or deleting an already-paid document. Only SUPER_ADMIN may
    cancel an ISSUED invoice; an ONBOARDER may cancel only their own DRAFTs
    (enforced by the caller's own-business scoping + this status check —
    a DRAFT has no external-facing numbered document to protect).
    """
    if invoice.status == "PAID":
        raise AppError("CANNOT_CANCEL_PAID", "A paid invoice cannot be cancelled", 409)
    if invoice.status == "CANCELLED":
        raise AppError("ALREADY_CANCELLED", "This invoice is already cancelled", 409)
    if invoice.status != "DRAFT" and not is_super_admin:
        raise AppError("FORBIDDEN", "Only SUPER_ADMIN may cancel an issued invoice", 403)

    invoice.status = "CANCELLED"
    invoice.cancelled_at = now or datetime.now(timezone.utc)
    invoice.cancelled_reason = reason


async def sweep_overdue_invoices(db: AsyncSession, *, today: date | None = None) -> int:
    """Flip every issued, unpaid invoice past its due date to OVERDUE. Run
    from the same hourly cron as billing/trial suspension
    (backend/app/worker.py). Returns the count changed, for logging."""
    today = today or datetime.now(timezone.utc).date()
    result = await db.execute(
        select(CommercialInvoice).where(
            CommercialInvoice.status.in_(("SENT", "AWAITING_PAYMENT")),
            CommercialInvoice.due_date < today,
        )
    )
    rows = result.scalars().all()
    for inv in rows:
        inv.status = "OVERDUE"
    return len(rows)
