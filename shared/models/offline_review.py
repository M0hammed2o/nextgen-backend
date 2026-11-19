"""
Late offline uploads awaiting owner/manager review (BUG-0208, migration 0069).

An offline POS capture that the automatic age bound refuses
(OFFLINE_CAPTURE_TOO_OLD: 36h for the event plan, 72h otherwise) is stored
here INSTEAD of being rejected, but only when the client opted in
(``offline_review_if_too_old: true`` on POST /business/pos/orders) and only
when the age bound was the ONLY reason to refuse it (an unpaid event day, a
future-dated capture, a missing timezone or an unknown cashier stay hard
refusals).

A PENDING row has no financial effect at all: nothing is posted to orders,
stock, payments or the till. An owner/manager either approves it (the order is
created then, through the same code path as a normal offline sync, re-validated
except for the age bound, and linked here via order_id) or rejects it (no
financial effect). ``payload`` is the validated POS order request exactly as
uploaded, so approval prices it server-side like any other sync.

Tenant-scoped by business_id (from the uploader's JWT). (business_id,
idempotency_key) is unique, and an approved row's order carries the same
idempotency_key, so one key can never produce two orders, or an order and a
pending review.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

OFFLINE_REVIEW_PENDING = "PENDING"
OFFLINE_REVIEW_APPROVED = "APPROVED"
OFFLINE_REVIEW_REJECTED = "REJECTED"


class OfflineCaptureReview(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "offline_capture_reviews"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "idempotency_key",
            name="uq_offline_capture_reviews_business_id_idempotency_key",
        ),
        Index("ix_offline_capture_reviews_business_id_status", "business_id", "status", "created_at"),
        CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')", name="status_valid"),
        CheckConstraint(
            "(status = 'PENDING') = (decided_at IS NULL)", name="decided_iff_not_pending",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OFFLINE_REVIEW_PENDING, server_default=OFFLINE_REVIEW_PENDING,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Device capture time (UTC), as uploaded",
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="The validated POST /business/pos/orders body, as uploaded",
    )
    captured_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="Cashier (business_users.id, not FK-enforced)",
    )
    synced_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="User whose session uploaded it",
    )
    synced_by_role: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="Uploader's role at upload time: approval keeps H-1 (min of uploader and cashier)",
    )
    upload_review_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]"),
        comment="Review reasons judged at upload time (copied onto the order on approval)",
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True,
    )
