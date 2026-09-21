"""Add whatsapp_connections table (customer-owned WhatsApp via Embedded Signup)

Revision ID: 0035_whatsapp_connections
Revises: 0033_order_number_uniqueness
Create Date: 2026-09-21

Wave 0A / Milestone C / DECISION-0022. Purely additive — a brand-new table,
no changes to any existing table or column. Fully reversible.

down_revision is pinned to 0033_order_number_uniqueness (the head at the
time this migration was authored in the wt-waba worktree) rather than
0034_onboarding_role, which is being developed in parallel in a sibling
worktree. The lead re-points this migration's down_revision at merge time
once both branches land — see the task brief.

One WhatsAppConnection row per business (UNIQUE business_id) and one row
per Meta phone_number_id (UNIQUE phone_number_id) — the latter prevents two
businesses from ever racing to claim the same Meta phone number at the DB
level, matching the existing uq on businesses.whatsapp_phone_number_id.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0035_whatsapp_connections"
down_revision = "0033_order_number_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nullable — a freshly-created PENDING row (before either id is
        # confirmed) has neither yet. NULL, not "", is required for
        # phone_number_id's uniqueness (Postgres allows multiple NULLs but
        # only one "" across every still-PENDING business's row).
        sa.Column("waba_id", sa.String(64), nullable=True),
        sa.Column("phone_number_id", sa.String(64), nullable=True),
        sa.Column("meta_business_id", sa.String(64), nullable=True),
        sa.Column("display_phone_number", sa.String(32), nullable=True),
        sa.Column("verified_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("status_reason", sa.Text, nullable=True),
        sa.Column("webhook_subscribed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("phone_registered", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("access_token_encrypted", sa.Text, nullable=True),
        sa.Column("registration_pin_encrypted", sa.Text, nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality_rating", sa.String(16), nullable=True),
        sa.Column("messaging_limit_tier", sa.String(32), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONNECTED', 'NEEDS_ATTENTION', 'DISCONNECTED')",
            name="ck_whatsapp_connections_status_known",
        ),
        sa.UniqueConstraint("business_id", name="uq_whatsapp_connections_business_id"),
        sa.UniqueConstraint("phone_number_id", name="uq_whatsapp_connections_phone_number_id"),
    )
    op.create_index(
        "ix_whatsapp_connections_business_id", "whatsapp_connections", ["business_id"]
    )
    op.create_index(
        "ix_whatsapp_connections_status", "whatsapp_connections", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_connections_status", table_name="whatsapp_connections")
    op.drop_index("ix_whatsapp_connections_business_id", table_name="whatsapp_connections")
    op.drop_table("whatsapp_connections")
