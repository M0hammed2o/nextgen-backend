"""
Billing invoice history — one row per successful/failed SaaS-billing charge,
created by whichever BillingProvider's webhook fires
(backend/app/billing/lifecycle.py). Distinct from the existing admin-suite
"Billing PDF" generator (a point-in-time summary document, still the
reusable renderer for these rows — see TODO.md for wiring it up per-invoice).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin


class BillingInvoice(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "billing_invoices"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False, comment="STRIPE / PAYFAST")
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="PAID / FAILED")
    plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<BillingInvoice {self.provider} {self.status} {self.amount_cents}c>"
