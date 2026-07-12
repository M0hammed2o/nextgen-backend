"""AI Email Outreach — lead + import batch tables

Revision ID: 0012_ai_leads
Revises: 0011_order_notes
Create Date: 2026-07-12

Super-admin-only lead-generation/outreach module (see "AI emails/" docs at the
repo root). Leads are external prospects (gyms/sports facilities), unrelated
to the `businesses` table (which is the platform's paying WhatsApp-bot tenants).

Phase 1 only: import batches + leads. No campaigns/generated-emails/sent-emails/
replies/followups/gmail-connections tables yet — those come in later phases.
"""
from alembic import op

revision = "0012_ai_leads"
down_revision = "0011_order_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Created first — ai_email_leads has an optional FK to it.
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_email_import_batches (
            id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            filename                    VARCHAR(255) NOT NULL,
            file_type                   VARCHAR(8) NOT NULL
                CHECK (file_type IN ('xlsx', 'csv')),
            status                      VARCHAR(16) NOT NULL DEFAULT 'previewed'
                CHECK (status IN ('previewed', 'confirmed', 'failed', 'expired')),
            uploaded_by_admin_user_id   UUID REFERENCES admin_users(id) ON DELETE SET NULL,
            column_mapping_json         JSONB NOT NULL,
            preview_rows_json           JSONB NOT NULL,
            total_rows                  INTEGER NOT NULL DEFAULT 0,
            valid_rows                  INTEGER NOT NULL DEFAULT 0,
            duplicate_rows              INTEGER NOT NULL DEFAULT 0,
            rejected_rows               INTEGER NOT NULL DEFAULT 0,
            created_count               INTEGER NOT NULL DEFAULT 0,
            updated_count               INTEGER NOT NULL DEFAULT 0,
            skipped_count               INTEGER NOT NULL DEFAULT 0,
            confirmed_at                TIMESTAMPTZ,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_import_batches_status
            ON ai_email_import_batches (status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_import_batches_uploaded_by
            ON ai_email_import_batches (uploaded_by_admin_user_id)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_email_leads (
            id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_name               VARCHAR(255) NOT NULL,
            category                    VARCHAR(128),
            city                        VARCHAR(128),
            suburb                      VARCHAR(128),
            address                     TEXT,
            phone                       VARCHAR(20),
            whatsapp                    VARCHAR(20),
            email                       VARCHAR(255),
            website                     VARCHAR(512),
            source_url                  VARCHAR(512),
            preferred_contact_method    VARCHAR(16) NOT NULL DEFAULT 'unknown'
                CHECK (preferred_contact_method IN ('email', 'whatsapp', 'phone', 'unknown')),
            verification_status        VARCHAR(16) NOT NULL DEFAULT 'unverified'
                CHECK (verification_status IN ('unverified', 'verified', 'invalid')),
            lead_status                 VARCHAR(24) NOT NULL DEFAULT 'new'
                CHECK (lead_status IN (
                    'new', 'requires_research', 'requires_verification', 'ready_to_generate',
                    'email_generated', 'awaiting_approval', 'draft_created', 'scheduled', 'sent',
                    'follow_up_due', 'replied', 'interested', 'demo_requested', 'not_interested',
                    'unsubscribed', 'invalid_email', 'do_not_contact', 'failed'
                )),
            research_notes              TEXT,
            ai_research_summary         TEXT,
            assigned_admin_user_id      UUID REFERENCES admin_users(id) ON DELETE SET NULL,
            tags                        TEXT[] NOT NULL DEFAULT '{}',
            do_not_contact              BOOLEAN NOT NULL DEFAULT FALSE,
            unsubscribed_at             TIMESTAMPTZ,
            unsubscribe_reason          TEXT,
            last_contacted_date         DATE,
            next_follow_up_date         DATE,
            import_batch_id             UUID REFERENCES ai_email_import_batches(id) ON DELETE SET NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_lead_status
            ON ai_email_leads (lead_status)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_city
            ON ai_email_leads (city)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_email_lower
            ON ai_email_leads (lower(email))
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_name_city_lower
            ON ai_email_leads (lower(business_name), lower(city))
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ai_email_leads_assigned
            ON ai_email_leads (assigned_admin_user_id)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_email_leads;")
    op.execute("DROP TABLE IF EXISTS ai_email_import_batches;")
