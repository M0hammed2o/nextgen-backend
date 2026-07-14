"""AI Email Outreach — phone outreach completion tracking

Revision ID: 0013_phone_outreach
Revises: 0012_ai_leads
Create Date: 2026-07-13

Leads without an email address are worked by phone/WhatsApp instead of
email — this column lets the operator tick a lead off once that manual
call is done, independent of the email-generation pipeline's lead_status.
"""
from alembic import op

revision = "0013_phone_outreach"
down_revision = "0012_ai_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE ai_email_leads
            ADD COLUMN IF NOT EXISTS phone_outreach_completed BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE ai_email_leads
            ADD COLUMN IF NOT EXISTS phone_outreach_completed_at TIMESTAMPTZ
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_phone_outreach_completed
            ON ai_email_leads (phone_outreach_completed)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE ai_email_leads DROP COLUMN IF EXISTS phone_outreach_completed_at;")
    op.execute("ALTER TABLE ai_email_leads DROP COLUMN IF EXISTS phone_outreach_completed;")
