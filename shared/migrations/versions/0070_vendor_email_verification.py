"""Souk vendor email verification: per-owner verification marker and single-use tokens

Revision ID: 0070_vendor_email_verification
Revises: 0069_offline_capture_review
Create Date: 2026-11-19

Chained after 0069_offline_capture_review on integration/souk-pilot (it was
written against 0068 on its own branch).

business_users:
- email_verification_required boolean NOT NULL default false. Every existing
  row (restaurants, admin-provisioned and bulk-onboarded owners, staff) gets
  false and is unaffected. Only POST /public/events/{slug}/register sets it.
- email_verified_at timestamptz NULL.
- registration_abandoned_at timestamptz NULL: a self-registration closed
  unverified after 7 days (soft close, nothing is deleted).

email_verification_tokens (new):
- token_hash is the SHA-256 hex of the raw token (the raw token is never
  stored). Single use via used_at; superseded tokens get invalidated_at.

All names go through op.f() (metadata naming convention).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0070_vendor_email_verification"
down_revision = "0069_offline_capture_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("business_users", sa.Column(
        "email_verification_required", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("business_users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("business_users", sa.Column(
        "registration_abandoned_at", sa.DateTime(timezone=True), nullable=True,
        comment="Set when a self-registration was closed unverified after 7 days (never a hard delete)"))

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["business_users.id"], ondelete="CASCADE",
                                name=op.f("fk_email_verification_tokens_user_id_business_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_verification_tokens_token_hash")),
    )
    op.create_index(op.f("ix_email_verification_tokens_user_id"), "email_verification_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_email_verification_tokens_user_id"), table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_column("business_users", "registration_abandoned_at")
    op.drop_column("business_users", "email_verified_at")
    op.drop_column("business_users", "email_verification_required")
