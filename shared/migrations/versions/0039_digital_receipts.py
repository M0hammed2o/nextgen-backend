"""Add digital_receipts table + Business receipt-delivery settings

Revision ID: 0039_digital_receipts
Revises: 0038_order_item_ad_hoc_price
Create Date: 2026-09-22

Digital Receipts, done properly (Commercial V1 pre-production gate).

Purely additive — one new table, three new nullable/default-false columns
on businesses. No existing table/column is altered or dropped. Fully
reversible.

digital_receipts: ONE row per order (UNIQUE order_id), the durable delivery
record read by both the staff-facing POS/manager receipt endpoints and the
public, unauthenticated token page (GET /v1/r/{token} — token alone is the
entire lookup key, business_id/order_id/customer_id are never accepted from
that route). See shared/models/digital_receipt.py for the full column
rationale and backend/app/receipts/whatsapp_receipt.py for how it's used.

businesses gains:
  - digital_receipts_enabled (bool, default False) — same "master switch"
    pattern as inventory_enabled. Gates the WhatsApp delivery channel only;
    printing a receipt is never gated by this (see DigitalReceipt.
    print_requested and DECISIONS.md).
  - whatsapp_receipt_template_name / whatsapp_receipt_template_language
    (nullable strings) — per-business override of the platform default
    Meta-approved template (RECEIPT_WHATSAPP_TEMPLATE_NAME/_LANGUAGE in
    backend/app/core/config.py), for a business whose own customer-owned
    WABA has a differently-named/localised approved template.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0039_digital_receipts"
down_revision = "0038_order_item_ad_hoc_price"
branch_labels = None
depends_on = None

_TABLE = "digital_receipts"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "order_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column(
            "customer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("phone_number_e164", sa.String(32), nullable=True),
        sa.Column("print_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("whatsapp_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("whatsapp_status", sa.String(16), nullable=False, server_default="NOT_REQUESTED"),
        sa.Column("whatsapp_failure_reason", sa.Text, nullable=True),
        sa.Column("whatsapp_wamid", sa.String(128), nullable=True),
        sa.Column("whatsapp_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("whatsapp_delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "whatsapp_status IN ('NOT_REQUESTED', 'QUEUED', 'SENT', 'DELIVERED', 'FAILED')",
            name="ck_digital_receipts_whatsapp_status_known",
        ),
        sa.UniqueConstraint("order_id", name="uq_digital_receipts_order_id"),
        sa.UniqueConstraint("token", name="uq_digital_receipts_token"),
    )
    op.create_index("ix_digital_receipts_business_id", _TABLE, ["business_id"])
    op.create_index("ix_digital_receipts_order_id", _TABLE, ["order_id"])
    op.create_index("ix_digital_receipts_token", _TABLE, ["token"])

    op.execute(
        "ALTER TABLE businesses "
        "ADD COLUMN IF NOT EXISTS digital_receipts_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE businesses "
        "ADD COLUMN IF NOT EXISTS whatsapp_receipt_template_name VARCHAR(512)"
    )
    op.execute(
        "ALTER TABLE businesses "
        "ADD COLUMN IF NOT EXISTS whatsapp_receipt_template_language VARCHAR(16)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_receipt_template_language")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_receipt_template_name")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS digital_receipts_enabled")
    op.drop_index("ix_digital_receipts_token", table_name=_TABLE)
    op.drop_index("ix_digital_receipts_order_id", table_name=_TABLE)
    op.drop_index("ix_digital_receipts_business_id", table_name=_TABLE)
    op.drop_table(_TABLE)
