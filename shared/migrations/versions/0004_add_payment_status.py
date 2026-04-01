"""Add payment_status to orders

Revision ID: 0004_add_payment_status
Revises: 0003_add_menu_image_url
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0004_add_payment_status"
down_revision = "0003_add_menu_image_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "payment_status",
            sa.String(32),
            nullable=False,
            server_default="PENDING",
            comment="PENDING / PAID / CASH_ON_COLLECTION",
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "payment_status")
