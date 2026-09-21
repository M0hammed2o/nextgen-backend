"""
Commercial (EFT/cash) invoices — NextGen billing a restaurant CLIENT for its
own subscription/setup/module fees via manual EFT or cash, issued by a sales
partner (ONBOARDER) or SUPER_ADMIN during the Wave 0A sales-onboarding push.

Deliberately NOT the same table as `billing_invoice.BillingInvoice`, which is
a payment-EVENT record written by a Stripe/PayFast webhook. A
CommercialInvoice is a real, numbered SOUTH AFRICAN TAX-RELEVANT DOCUMENT with
its own lifecycle (draft -> issued -> paid/cancelled); marking one PAID also
writes a `BillingInvoice(provider="EFT"|"CASH", ...)` row so a business's
full payment history (self-service Stripe/PayFast charges AND manually
recorded EFT/cash payments) stays in one place for
`GET /v1/business/billing/invoices` — see
backend/app/billing/commercial_invoices.py::mark_paid.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

INVOICE_STATUSES = ("DRAFT", "SENT", "AWAITING_PAYMENT", "PAID", "OVERDUE", "CANCELLED")
# Statuses that mean "issued and not yet paid" — the derived account-status
# function and the go-live/dashboard "amount outstanding" figures both treat
# these identically (see backend/app/billing/account_status.py).
UNPAID_ISSUED_STATUSES = ("SENT", "AWAITING_PAYMENT", "OVERDUE")
INVOICE_PAYMENT_METHODS = ("EFT", "CASH")
INVOICE_LINE_KINDS = ("SETUP", "SUBSCRIPTION", "MODULE", "OTHER")


class CommercialInvoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "commercial_invoices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','SENT','AWAITING_PAYMENT','PAID','OVERDUE','CANCELLED')",
            name="ck_commercial_invoices_status",
        ),
        CheckConstraint(
            "payment_method IS NULL OR payment_method IN ('EFT','CASH')",
            name="ck_commercial_invoices_payment_method",
        ),
    )

    # RESTRICT, not CASCADE: a business with any invoice history (even a
    # cancelled one) must never be silently deletable — there is no route in
    # this codebase that hard-deletes a Business today, but this is the
    # defense-in-depth choice per the task brief.
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    invoice_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, nullable=True,
        comment="NG-INV-{YYYY}-{seq:05d}, assigned only when a draft is issued "
                "(drafts stay NULL so an abandoned draft never creates a gap "
                "in the sequential numbering a SA tax invoice needs).",
    )
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", nullable=False)

    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="ZAR", nullable=False)

    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vat_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vat_rate_basis_points: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    payment_method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    internal_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Snapshotted at issue time so a later change to the business's own
    # details, or to NextGen's own config, never alters an already-issued
    # invoice document.
    customer_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    supplier_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True,
    )
    paid_recorded_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True,
    )

    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    lines = relationship(
        "CommercialInvoiceLine", back_populates="invoice",
        order_by="CommercialInvoiceLine.sort_order", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<CommercialInvoice {self.invoice_number or '(draft)'} {self.status}>"


class CommercialInvoiceLine(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "commercial_invoice_lines"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('SETUP','SUBSCRIPTION','MODULE','OTHER')",
            name="ck_commercial_invoice_lines_kind",
        ),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commercial_invoices.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="OTHER", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    invoice = relationship("CommercialInvoice", back_populates="lines")
