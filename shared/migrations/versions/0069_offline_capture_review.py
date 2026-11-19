"""Late offline uploads awaiting review: offline_capture_reviews

Revision ID: 0069_offline_capture_review
Revises: 0068_offline_review_hardening
Create Date: 2026-11-19

BUG-0208. An offline POS capture refused ONLY because it is older than the
automatic age bound (OFFLINE_CAPTURE_TOO_OLD) is stored here, when the client
opts in, instead of being rejected. A pending row has no financial effect; an
owner/manager approves (the order is created then) or rejects it. See
shared/models/offline_review.py.

New table only; no existing row or column changes. Every name goes through
op.f() (metadata naming convention).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069_offline_capture_review"
down_revision = "0068_offline_review_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offline_capture_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("captured_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("synced_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("synced_by_role", sa.String(20), nullable=False),
        sa.Column("upload_review_reasons", postgresql.ARRAY(sa.Text()),
                  server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offline_capture_reviews")),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE",
                                name=op.f("fk_offline_capture_reviews_business_id_businesses")),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL",
                                name=op.f("fk_offline_capture_reviews_order_id_orders")),
        sa.UniqueConstraint("business_id", "idempotency_key",
                            name=op.f("uq_offline_capture_reviews_business_id_idempotency_key")),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')",
                           name=op.f("ck_offline_capture_reviews_status_valid")),
        sa.CheckConstraint("(status = 'PENDING') = (decided_at IS NULL)",
                           name=op.f("ck_offline_capture_reviews_decided_iff_not_pending")),
    )
    op.create_index(op.f("ix_offline_capture_reviews_business_id_status"), "offline_capture_reviews",
                    ["business_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_offline_capture_reviews_business_id_status"), table_name="offline_capture_reviews")
    op.drop_table("offline_capture_reviews")
