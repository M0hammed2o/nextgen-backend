"""POS order-ready notification consent; WhatsApp off for event vendors

Revision ID: 0065_order_notify_when_ready
Revises: 0064_event_self_registration
Create Date: 2026-11-18

Adds:
- orders.notify_when_ready BOOLEAN NOT NULL DEFAULT false: the cashier recorded
  the customer's explicit consent to a WhatsApp message when a POS order is
  READY. A phone number captured for loyalty is not consent.

Data fix (security review H1): event-plan businesses get is_whatsapp_enabled =
false. WhatsApp-originated orders bypass the souk trading gate, so the channel
stays off for event vendors until the platform approves it. Not reversed on
downgrade (the prior value is not recoverable and true was never intended).
"""
import sqlalchemy as sa
from alembic import op

revision = "0065_order_notify_when_ready"
down_revision = "0064_event_self_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("notify_when_ready", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    op.execute("UPDATE businesses SET is_whatsapp_enabled = false WHERE plan = 'EVENT'")


def downgrade() -> None:
    op.drop_column("orders", "notify_when_ready")
