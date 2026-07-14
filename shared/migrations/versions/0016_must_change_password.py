"""Forced password reset flag for owner/manager accounts

Revision ID: 0016_must_change_password
Revises: 0015_whatsapp_pause
Create Date: 2026-07-13

When a super-admin creates an OWNER/MANAGER login, the system now generates
a temporary password and sets this flag. The owner's next email+password
login is blocked (PASSWORD_CHANGE_REQUIRED) until they set a real password
via POST /auth/set-password.
"""
from alembic import op

revision = "0016_must_change_password"
down_revision = "0015_whatsapp_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE business_users
            ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE business_users
            DROP COLUMN IF EXISTS must_change_password
    """)
