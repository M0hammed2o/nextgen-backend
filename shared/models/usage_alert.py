"""
WhatsApp order-usage threshold alerts (migration 0057_pos_entitlement).

A deliberately NEW, minimal model rather than reusing the existing
`AuditEvent` table. `AuditEvent` is a general, append-only, free-form
(`diff_json`) audit trail with no natural "has threshold X already been
recorded for business Y's CURRENT billing period" query — answering that
from `AuditEvent` would mean filtering on `action` string plus the most
recent row per business, with no DB-enforced uniqueness backstop. This
table gets a real partial-like uniqueness guarantee instead (see
`uq_usage_alerts_business_period_threshold` on the migration) so the daily
cron job (`backend/app/worker.py::check_whatsapp_usage_alerts`) can never
double-write the same (business, billing_period, threshold) combination,
even under a retry/overlap. See DECISIONS.md for this choice recorded in
full.

Read-only from the application's perspective outside that one cron job —
nothing else writes to this table.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UsageAlert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "usage_alerts"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "billing_period_started_at", "threshold_crossed",
            name="uq_usage_alerts_business_period_threshold",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Snapshot of Business.billing_period_started_at at the moment this
    # alert was recorded — NOT a live FK-like reference. A business's
    # billing period resets (new value) when a subscription invoice is
    # paid; this column anchors the alert to the specific period it was
    # raised in, so a new period always starts threshold-crossing fresh.
    billing_period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    threshold_crossed: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="80, 90, or 100 — the whatsapp_order_allowance percentage "
                "threshold newly crossed at the time this row was written.",
    )
    orders_used: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Snapshot of orders_used (billable WhatsApp orders this "
                "period) at the moment the threshold was crossed.",
    )
    orders_allowance: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Snapshot of business.whatsapp_order_allowance at the "
                "moment the threshold was crossed.",
    )

    def __repr__(self) -> str:
        return f"<UsageAlert business={self.business_id} threshold={self.threshold_crossed}%>"
