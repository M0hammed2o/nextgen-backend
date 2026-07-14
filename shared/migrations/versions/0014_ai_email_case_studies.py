"""AI Email Outreach — structured case-study profiles

Revision ID: 0014_case_studies
Revises: 0013_phone_outreach
Create Date: 2026-07-12

Structured, approved facts for case-study reference projects (starting with
Muscle Factory), so the Phase 2 email generator reads facts from a table
instead of only from prompt text. No dependency on `businesses`.

approved_for_marketing seeded true for Muscle Factory: Mohammed (NextGen's
principal) explicitly directed it as the primary case study for this
campaign — that is treated as NextGen's own internal editorial approval.
client_permission_status is seeded 'not_requested': whether Muscle Factory
(the client business) has consented to being named/referenced with third
parties has not been confirmed in any conversation, so it is not assumed.
"""
from alembic import op

revision = "0014_case_studies"
down_revision = "0013_phone_outreach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_email_case_studies (
            id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            case_study_name                 VARCHAR(255) NOT NULL,
            client_name                     VARCHAR(255) NOT NULL,
            industry                        VARCHAR(128),
            approved_for_marketing          BOOLEAN NOT NULL DEFAULT FALSE,
            confirmed_deployed_features     JSONB NOT NULL DEFAULT '[]',
            optional_or_proposed_features   JSONB NOT NULL DEFAULT '[]',
            approved_credibility_statement  TEXT,
            approved_screenshots            JSONB NOT NULL DEFAULT '[]',
            screenshot_redaction_status     VARCHAR(16) NOT NULL DEFAULT 'not_applicable'
                CHECK (screenshot_redaction_status IN ('not_applicable', 'pending', 'completed')),
            client_permission_status        VARCHAR(16) NOT NULL DEFAULT 'not_requested'
                CHECK (client_permission_status IN ('not_requested', 'requested', 'granted', 'denied')),
            last_reviewed_date              DATE,
            created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        INSERT INTO ai_email_case_studies (
            case_study_name, client_name, industry,
            approved_for_marketing,
            confirmed_deployed_features,
            optional_or_proposed_features,
            approved_credibility_statement,
            approved_screenshots,
            screenshot_redaction_status,
            client_permission_status,
            last_reviewed_date
        ) VALUES (
            'Muscle Factory Gym Management System',
            'Muscle Factory',
            'Gym / Fitness Centre',
            TRUE,
            '[
                "Staff can register and load clients into the system.",
                "A client''s fingerprint can be enrolled during registration using compatible fingerprint hardware.",
                "Once enrolled, the client scans their finger to enter the gym; the system identifies them and records the visit.",
                "Staff can view client information and attendance history.",
                "Two payment arrangements are supported: pay-per-day, and monthly membership.",
                "The system can track the client''s selected payment method/membership arrangement.",
                "The system assists with customer accounts and gym administration."
            ]'::jsonb,
            '[
                "Full subscriptions and recurring membership periods",
                "Month-end statements",
                "Automated customer emails"
            ]'::jsonb,
            'We recently developed a custom management system for Muscle Factory based on the way their gym operates. Their system allows staff to register members, enrol them using compatible fingerprint hardware and record attendance when members scan in. It also supports the two payment models they required: daily access and monthly memberships.',
            '[]'::jsonb,
            'not_applicable',
            'not_requested',
            '2026-07-12'
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_email_case_studies;")
