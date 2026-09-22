"""
Digital receipts — one row per order, the durable record of "was a
customer-facing receipt requested for this order, and did the WhatsApp
delivery of it succeed".

WHY ITS OWN TABLE, NOT A REUSE OF message_outbox
-------------------------------------------------
message_outbox is a generic retry queue for conversational WhatsApp text
sends (many rows can exist for one order — every bot reply, every status
notification). A digital receipt is structurally different: there is
exactly one meaningful receipt per order (retried in place, never
duplicated), it needs a stable PUBLIC token a customer can revisit anytime
(message_outbox has no such concept), and its delivery status needs to
survive independently of message_outbox's own attempts/backoff bookkeeping,
which is tuned for a different retry philosophy (automatic background
retry) than a receipt's (explicit staff-triggered resend — see
backend/app/receipts/whatsapp_receipt.py::send_digital_receipt).
Consolidating those two into one table/model would have made both worse to
reason about; this table is intentionally the ONE durable record for
receipt delivery, not a second parallel outbox.

token is the entire, sufficient lookup key for the public, unauthenticated
receipt page (GET /v1/r/{token}) — see backend/app/receipts/token.py for
its entropy — business_id/order_id/customer_id are never accepted from
that route's URL/query, only read off this row after the token resolves it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

WHATSAPP_RECEIPT_STATUSES = (
    "NOT_REQUESTED",
    "QUEUED",
    "SENT",
    "DELIVERED",
    "FAILED",
)


class DigitalReceipt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "digital_receipts"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="Tenant scope — used for staff/manager queries (usage totals, "
                "resend authorisation). NEVER exposed on the public token page.",
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
        comment="One receipt row per order — get-or-create, never a second row",
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
        comment="High-entropy, non-sequential public lookup key — see "
                "backend/app/receipts/token.py::generate_receipt_token()",
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        comment="Set only when a Customer row already existed (WhatsApp order, "
                "or POS order already linked for loyalty) — never created solely "
                "to deliver a receipt.",
    )
    phone_number_e164: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="Number actually used for WhatsApp delivery — kept here so a "
                "walk-in POS customer's receipt does not require a permanent "
                "Customer row",
    )

    print_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whatsapp_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    whatsapp_status: Mapped[str] = mapped_column(
        String(16), default="NOT_REQUESTED", nullable=False,
        comment="NOT_REQUESTED / QUEUED / SENT / DELIVERED / FAILED",
    )
    whatsapp_failure_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Safe, classified failure reason only — never a token/secret",
    )
    whatsapp_wamid: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="Meta message id — for future delivery-webhook correlation",
    )
    whatsapp_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    whatsapp_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="business_users.id of who requested this at the till/dashboard — "
                "not FK-enforced, same pattern as Order.cancelled_by_user_id",
    )

    def __repr__(self) -> str:
        return f"<DigitalReceipt order={self.order_id} whatsapp={self.whatsapp_status}>"
