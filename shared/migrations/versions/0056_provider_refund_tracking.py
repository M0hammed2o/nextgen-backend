"""Track provider-refund honesty for POS Pay-by-Link allocations

Revision ID: 0056_provider_refund_tracking
Revises: 0055_pos_payment_allocations
Create Date: 2026-10-11

Financial-safety closeout (Issue 3, following the local browser release
gate): a till-native refund/void on an order with a PAY_BY_LINK allocation
previously recorded the refund in NextGen with no signal that the
customer's actual provider payment (Yoco/PayFast) was never touched --
this platform has no verified, safe way to call a real provider refund API
from the till-refund context (see docs/SPLIT_PAYMENTS_CONTRACT.md's
provider-refund-honesty section for the full reasoning), so the UI must
say so explicitly instead of implying the money was returned automatically.

Purely additive, fully reversible.
"""
from alembic import op
import sqlalchemy as sa

revision = "0056_provider_refund_tracking"
down_revision = "0055_pos_payment_allocations"
branch_labels = None
depends_on = None

_TABLE = "pos_payment_allocations"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "provider_refund_status VARCHAR(16) NULL"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "provider_refund_confirmed_at TIMESTAMPTZ NULL"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "provider_refund_confirmed_by_user_id UUID NULL"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT ck_pos_payment_allocations_provider_refund_status "
        "CHECK (provider_refund_status IS NULL OR provider_refund_status IN "
        "('PENDING_MANUAL', 'CONFIRMED'))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS ck_pos_payment_allocations_provider_refund_status")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS provider_refund_confirmed_by_user_id")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS provider_refund_confirmed_at")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS provider_refund_status")
