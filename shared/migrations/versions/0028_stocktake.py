"""Stocktake sessions and count lines with snapshotted variance

Revision ID: 0028_stocktake
Revises: 0027_wastage
Create Date: 2026-09-07

Ciao Pizza P0, part 5 of 5.

Modelled on till_sessions (0017): open, capture counts, compute variance,
manager sign-off, then lock one-way. An APPROVED session is immutable.

`cutoff_at` is what makes a count meaningful during trading — only ledger
movements with occurred_at <= cutoff_at belong to the period being counted,
so a sale rung up while staff are still counting falls into the next period
instead of silently corrupting this one.

Expected/theoretical figures are snapshotted onto each line at completion so
the variance a manager signed off can always be reproduced exactly, even
after the ledger gains later compensating corrections.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_stocktake"
down_revision = "0027_wastage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stocktake_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reference", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_variance_value_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_theoretical_value_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'COMPLETED', 'APPROVED', 'CANCELLED')",
            name="ck_stocktake_sessions_status_known",
        ),
    )
    op.create_index("ix_stocktake_sessions_business_id", "stocktake_sessions", ["business_id"])
    op.create_index("ix_stocktake_sessions_status", "stocktake_sessions", ["status"])
    op.create_index(
        "ix_stocktake_sessions_business_status", "stocktake_sessions", ["business_id", "status"]
    )
    op.create_index(
        "ix_stocktake_sessions_business_cutoff", "stocktake_sessions", ["business_id", "cutoff_at"]
    )
    # At most one count in progress per business — two concurrent DRAFT
    # counts would produce two different "expected" figures for one period.
    op.execute("""
        CREATE UNIQUE INDEX uq_stocktake_sessions_one_open_per_business
            ON stocktake_sessions (business_id)
            WHERE status = 'DRAFT'
    """)

    op.create_table(
        "stocktake_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stocktake_session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("stocktake_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ingredient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("counted_quantity_milli", sa.BigInteger, nullable=True),
        sa.Column("entered_quantity", sa.String(32), nullable=True),
        sa.Column("entered_unit", sa.String(32), nullable=True),
        sa.Column("count_method_snapshot", sa.String(16), nullable=True),
        sa.Column("opening_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("received_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("theoretical_consumption_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("wastage_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("adjustment_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("expected_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("variance_quantity_milli", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("variance_value_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("variance_basis_points", sa.Integer, nullable=True),
        sa.Column("unit_cost_micro_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "stocktake_session_id",
            "ingredient_id",
            name="uq_stocktake_lines_stocktake_session_id_ingredient_id",
        ),
    )
    op.create_index("ix_stocktake_lines_business_id", "stocktake_lines", ["business_id"])
    op.create_index("ix_stocktake_lines_business", "stocktake_lines", ["business_id"])
    op.create_index(
        "ix_stocktake_lines_stocktake_session_id", "stocktake_lines", ["stocktake_session_id"]
    )


def downgrade() -> None:
    op.drop_table("stocktake_lines")
    op.execute("DROP INDEX IF EXISTS uq_stocktake_sessions_one_open_per_business")
    op.drop_table("stocktake_sessions")
