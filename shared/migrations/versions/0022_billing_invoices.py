"""Billing invoice history

Revision ID: 0022_billing_invoices
Revises: 0021_billing_provider_abstraction
Create Date: 2026-07-24

One row per successful/failed SaaS-billing charge, created by
backend/app/billing/lifecycle.py regardless of which BillingProvider fired
the webhook. "Store invoice history. Show invoices inside the Manager
Dashboard." — PDF rendering/email delivery are separate, larger pieces
(see KNOWN_BUGS.md/TODO.md); this migration is the storage layer.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022_billing_invoices"
down_revision = "0021_billing_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("plan", sa.String(32), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_invoices_business_id", "billing_invoices", ["business_id"])


def downgrade() -> None:
    op.drop_index("ix_billing_invoices_business_id", table_name="billing_invoices")
    op.drop_table("billing_invoices")
