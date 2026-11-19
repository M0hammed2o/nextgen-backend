"""Offline POS capture: orders carry when/by whom an offline sale really happened

Revision ID: 0067_offline_pos_capture
Revises: 0066_product_variants
Create Date: 2026-11-19

Adds to orders (all nullable or defaulted, so every existing row and every
online sale is unchanged):
- is_offline_capture / offline_captured_at: the sale was rung up on a device
  while offline, at this (client-reported, 72h-bounded) time.
- offline_synced_at / offline_synced_by_user_id: when and by whom the queued
  sale reached the server (created_by_user_id stays the cashier who made it).
- offline_unattributed: no till session covered offline_captured_at, so the
  sale was attached to none (it is surfaced, never put in the wrong session).
- offline_late_to_closed_till: attributed to a till session that had already
  been closed when the sale synced (that session's stored cash-up predates it).
- offline_stock_shortfall: the sale took an ingredient/variant stock negative.
- offline_client_total_cents / offline_price_variance_cents: the total the
  device displayed and (client minus server) when they differ. The server's
  price is always the one charged.
- offline_review_required: any flag above is set; drives the manager list.

Every constraint/index name is passed through op.f() (metadata naming convention).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067_offline_pos_capture"
down_revision = "0066_product_variants"
branch_labels = None
depends_on = None

_BOOLS = ("is_offline_capture", "offline_unattributed", "offline_late_to_closed_till",
          "offline_stock_shortfall", "offline_review_required")


def upgrade() -> None:
    for col in _BOOLS:
        op.add_column("orders", sa.Column(col, sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("orders", sa.Column("offline_captured_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("offline_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("offline_synced_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("orders", sa.Column("offline_client_total_cents", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("offline_price_variance_cents", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_offline_capture_has_time"), "orders",
        "NOT is_offline_capture OR offline_captured_at IS NOT NULL",
    )
    op.create_index(
        op.f("ix_orders_business_id_offline_flagged"), "orders", ["business_id", "offline_captured_at"],
        postgresql_where=sa.text("offline_review_required"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_orders_business_id_offline_flagged"), table_name="orders")
    op.drop_constraint(op.f("ck_orders_offline_capture_has_time"), "orders", type_="check")
    for col in ("offline_price_variance_cents", "offline_client_total_cents", "offline_synced_by_user_id",
                "offline_synced_at", "offline_captured_at", *reversed(_BOOLS)):
        op.drop_column("orders", col)
