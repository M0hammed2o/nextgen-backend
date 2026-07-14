"""WhatsApp pause/busy toggle

Revision ID: 0015_whatsapp_pause
Revises: 0014_case_studies
Create Date: 2026-07-13

Lets staff pause incoming WhatsApp orders during a rush, with a
business-customizable message shown to customers while paused — the same
pattern as the existing closed_text/closed-for-the-day gate.

Deliberately separate from is_whatsapp_enabled (the platform-admin kill
switch): a staff-facing pause must never be able to undo an admin-level
suspension, so the two flags stay independent and admin's flag stays
authoritative.
"""
from alembic import op

revision = "0015_whatsapp_pause"
down_revision = "0014_case_studies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS whatsapp_paused BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN IF NOT EXISTS whatsapp_paused_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS whatsapp_paused_by_user_id UUID NULL,
            ADD COLUMN IF NOT EXISTS busy_text TEXT NULL
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE businesses
            DROP COLUMN IF EXISTS whatsapp_paused,
            DROP COLUMN IF EXISTS whatsapp_paused_at,
            DROP COLUMN IF EXISTS whatsapp_paused_by_user_id,
            DROP COLUMN IF EXISTS busy_text
    """)
