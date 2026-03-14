"""Model 1: Single WABA architecture

Drop whatsapp_access_token_encrypted (platform token in env now).
Add is_whatsapp_enabled toggle per business.

Revision ID: 0002_model1_single_waba
Revises: 0001_initial_schema
Create Date: 2026-03-03
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = "0002_model1_single_waba"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop per-business access token column (Model 1: platform token lives in env)
    op.drop_column("businesses", "whatsapp_access_token_encrypted")

    # Add WhatsApp enabled toggle per business
    op.add_column(
        "businesses",
        sa.Column(
            "is_whatsapp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="Toggle WhatsApp bot on/off per business",
        ),
    )


def downgrade() -> None:
    # Re-add per-business token column
    op.add_column(
        "businesses",
        sa.Column(
            "whatsapp_access_token_encrypted",
            sa.Text(),
            nullable=True,
            comment="Encrypted Meta access token",
        ),
    )

    # Drop is_whatsapp_enabled
    op.drop_column("businesses", "is_whatsapp_enabled")
