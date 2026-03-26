"""Add menu_image_url to businesses

Revision ID: 0003_add_menu_image_url
Revises: 0002_model1_single_waba
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0003_add_menu_image_url"
down_revision = "0002_model1_single_waba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "menu_image_url",
            sa.Text(),
            nullable=True,
            comment="Public URL of menu image to send via WhatsApp when customer requests menu",
        ),
    )


def downgrade() -> None:
    op.drop_column("businesses", "menu_image_url")
