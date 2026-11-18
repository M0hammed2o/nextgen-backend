"""Souk vendor self-registration and payment claims

Revision ID: 0064_event_self_registration
Revises: 0063_event_access_levels
Create Date: 2026-11-18

Adds:
- events.registration_open BOOLEAN NOT NULL DEFAULT false: vendors may
  self-register only while an administrator has opened registration.
- events.registration_code_hash VARCHAR(255) NULL: bcrypt hash of the shared
  registration code. The plaintext is shown once to the administrator and
  never stored.
- event_vendor_subscriptions.payment_claim_reference VARCHAR(255) NULL and
  payment_claimed_at TIMESTAMPTZ NULL: the reference a vendor says it paid
  with. A claim never marks anything paid; an administrator confirms it with
  the existing mark-paid route.

Downgrade drops the four columns (claims and the registration code are lost).
"""
import sqlalchemy as sa
from alembic import op

revision = "0064_event_self_registration"
down_revision = "0063_event_access_levels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("registration_open", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    op.add_column("events", sa.Column("registration_code_hash", sa.String(255), nullable=True))
    op.add_column("event_vendor_subscriptions",
                  sa.Column("payment_claim_reference", sa.String(255), nullable=True))
    op.add_column("event_vendor_subscriptions",
                  sa.Column("payment_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("event_vendor_subscriptions", "payment_claimed_at")
    op.drop_column("event_vendor_subscriptions", "payment_claim_reference")
    op.drop_column("events", "registration_code_hash")
    op.drop_column("events", "registration_open")
