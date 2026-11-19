"""Souk security review: offline review reasons, owner acknowledgement, return restock snapshot

Revision ID: 0068_offline_review_hardening
Revises: 0067_offline_pos_capture
Create Date: 2026-11-19

orders:
- offline_review_reasons text[] NOT NULL default '{}': review reasons that are
  not already a boolean (capture_day_unpaid_at_sync, late_sync,
  cashier_differs_from_syncer). Existing rows get the empty array.
- offline_acknowledged_at / offline_acknowledged_by_user_id: an OWNER has
  reviewed the flagged sale (POST /business/pos/offline-orders/{id}/acknowledge).

order_items:
- stock_ingredient_id_snapshot: the ingredient a variant line actually
  consumed at sale time, so a partial-return restock goes back to it even if
  the variant is re-linked afterwards. NULL on pre-0068 lines (the return code
  falls back to the variant's current link, the old behaviour).

All names go through op.f() (metadata naming convention).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068_offline_review_hardening"
down_revision = "0067_offline_pos_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column(
        "offline_review_reasons", postgresql.ARRAY(sa.Text()),
        server_default=sa.text("'{}'::text[]"), nullable=False))
    op.add_column("orders", sa.Column("offline_acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("offline_acknowledged_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("order_items", sa.Column("stock_ingredient_id_snapshot", postgresql.UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("order_items", "stock_ingredient_id_snapshot")
    op.drop_column("orders", "offline_acknowledged_by_user_id")
    op.drop_column("orders", "offline_acknowledged_at")
    op.drop_column("orders", "offline_review_reasons")
