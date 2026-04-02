"""Add UNIQUE constraint on conversation_sessions(business_id, customer_id)

Revision ID: 0005_unique_conversation_session
Revises: 0004_add_payment_status
Create Date: 2026-04-01

Removes duplicate session rows (keeping most recently active) then adds
unique constraint so concurrent webhook delivery cannot create split-cart rows.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0005_unique_conversation_session"
down_revision = "0004_add_payment_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove duplicate rows — keep the one with the latest last_activity_at.
    # Using ctid (Postgres physical row id) as tiebreaker when timestamps match.
    op.execute("""
        DELETE FROM conversation_sessions a
        USING conversation_sessions b
        WHERE a.business_id = b.business_id
          AND a.customer_id = b.customer_id
          AND a.last_activity_at < b.last_activity_at
    """)
    # Second pass: if any duplicates remain (identical timestamps), keep lowest id
    op.execute("""
        DELETE FROM conversation_sessions a
        USING conversation_sessions b
        WHERE a.business_id = b.business_id
          AND a.customer_id = b.customer_id
          AND a.id < b.id
          AND a.last_activity_at = b.last_activity_at
    """)

    op.create_unique_constraint(
        "uq_conversation_sessions_business_customer",
        "conversation_sessions",
        ["business_id", "customer_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_sessions_business_customer",
        "conversation_sessions",
        type_="unique",
    )
