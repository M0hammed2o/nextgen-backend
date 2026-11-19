"""Souk WhatsApp gate: order-ready template per business + notify consent provenance

Revision ID: 0071_event_wa_ready_template
Revises: 0068_offline_review_hardening
Create Date: 2026-11-19

businesses:
- whatsapp_ready_template_name / _language: the order-ready template the
  business submitted in its OWN WhatsApp Manager (NextGen never creates
  templates through the Meta API).
- whatsapp_ready_template_approved bool NOT NULL default false: set only by a
  SUPER_ADMIN after confirming Meta approved the template on that WABA.
  Existing rows: false (nothing is sent by template until confirmed).

orders:
- notify_consent_at / notify_consent_by_user_id: who recorded the POS
  notify_when_ready consent, and when. NULL on pre-0071 orders.

Additive only; no data rewritten. No constraint or index is created, so there
are no op.f() names to generate.
NOTE: down_revision is 0068 on this branch; parallel branches add 0069/0070,
so this is re-pointed at merge time. The id is shortened from the planned
"0071_event_whatsapp_ready_template" (34 chars) because alembic_version.version_num
is varchar(32); the longer id failed the upgrade with StringDataRightTruncationError.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071_event_wa_ready_template"
down_revision = "0068_offline_review_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("businesses", sa.Column("whatsapp_ready_template_name", sa.String(length=512), nullable=True))
    op.add_column("businesses", sa.Column("whatsapp_ready_template_language", sa.String(length=16), nullable=True))
    op.add_column("businesses", sa.Column(
        "whatsapp_ready_template_approved", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("orders", sa.Column("notify_consent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("notify_consent_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "notify_consent_by_user_id")
    op.drop_column("orders", "notify_consent_at")
    op.drop_column("businesses", "whatsapp_ready_template_approved")
    op.drop_column("businesses", "whatsapp_ready_template_language")
    op.drop_column("businesses", "whatsapp_ready_template_name")
