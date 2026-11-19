"""Temporary events (souk) foundation + businesses.industry

Revision ID: 0062_events_foundation
Revises: 0061_business_user_token_version
Create Date: 2026-11-18

Adds:
- businesses.industry (NOT NULL DEFAULT 'RESTAURANT', checked). Every
  existing business reads RESTAURANT, which is what each of them is today.
- events, event_days, event_vendors, event_vendor_subscriptions
  (shared/models/event.py).

Purely additive: no existing column, constraint or row changes meaning.
Every constraint name is passed through op.f() so the metadata naming
convention is not applied twice (the double-prefix trap behind BUG-0148).

Downgrade drops the four new tables (event data is lost) and the industry
column (every business returns to the implicit restaurant platform).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062_events_foundation"
down_revision = "0061_business_user_token_version"
branch_labels = None
depends_on = None

_INDUSTRIES = (
    "RESTAURANT", "CAFE", "TAKEAWAY", "BUTCHERY", "BAKERY",
    "CLOTHING_RETAIL", "GENERAL_RETAIL", "EVENT_FOOD", "EVENT_RETAIL",
)


def _ts():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("industry", sa.String(32), nullable=False, server_default="RESTAURANT"),
    )
    op.create_check_constraint(
        op.f("ck_businesses_industry_known"),
        "businesses",
        "industry IN (" + ",".join(f"'{i}'" for i in _INDUSTRIES) + ")",
    )

    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("venue", sa.String(255), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Johannesburg"),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("food_day_rate_cents", sa.Integer(), nullable=False),
        sa.Column("clothing_day_rate_cents", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("slug", name=op.f("uq_events_slug")),
        sa.CheckConstraint("status IN ('DRAFT','OPEN','CLOSED','CANCELLED')", name=op.f("ck_events_status_known")),
        sa.CheckConstraint(
            "food_day_rate_cents >= 0 AND clothing_day_rate_cents >= 0", name=op.f("ck_events_rates_non_negative")
        ),
    )

    op.create_table(
        "event_days",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("is_billable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_days")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], ondelete="CASCADE", name=op.f("fk_event_days_event_id_events")
        ),
        sa.UniqueConstraint("event_id", "day", name=op.f("uq_event_days_event_id_day")),
    )

    op.create_table(
        "event_vendors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("stall_code", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_vendors")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], ondelete="CASCADE", name=op.f("fk_event_vendors_event_id_events")
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE",
            name=op.f("fk_event_vendors_business_id_businesses"),
        ),
        sa.UniqueConstraint("event_id", "business_id", name=op.f("uq_event_vendors_event_id_business_id")),
        sa.CheckConstraint("category IN ('FOOD','CLOTHING')", name=op.f("ck_event_vendors_category_known")),
        sa.CheckConstraint(
            "status IN ('PENDING','ACTIVE','UNPAID','EXPIRED','CANCELLED')", name=op.f("ck_event_vendors_status_known")
        ),
    )
    op.create_index(op.f("ix_event_vendors_business_id"), "event_vendors", ["business_id"])
    op.create_index(
        op.f("uq_event_vendors_event_id_stall_code"), "event_vendors", ["event_id", "stall_code"],
        unique=True, postgresql_where=sa.text("stall_code IS NOT NULL"),
    )

    op.create_table(
        "event_vendor_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_vendor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("day_rate_cents", sa.Integer(), nullable=False),
        sa.Column("billable_days", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="UNPAID"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String(255), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_vendor_subscriptions")),
        sa.ForeignKeyConstraint(
            ["event_vendor_id"], ["event_vendors.id"], ondelete="CASCADE",
            name=op.f("fk_event_vendor_subscriptions_event_vendor_id_event_vendors"),
        ),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="CASCADE",
            name=op.f("fk_event_vendor_subscriptions_business_id_businesses"),
        ),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f("ck_event_vendor_subscriptions_range_ordered")),
        sa.CheckConstraint(
            "day_rate_cents >= 0 AND billable_days >= 0", name=op.f("ck_event_vendor_subscriptions_non_negative")
        ),
        sa.CheckConstraint(
            "amount_cents = day_rate_cents * billable_days", name=op.f("ck_event_vendor_subscriptions_amount_matches")
        ),
        sa.CheckConstraint(
            "status IN ('UNPAID','PAID','VOID')", name=op.f("ck_event_vendor_subscriptions_status_known")
        ),
        sa.CheckConstraint(
            "(status = 'PAID') = (paid_at IS NOT NULL)", name=op.f("ck_event_vendor_subscriptions_paid_at_iff_paid")
        ),
    )
    op.create_index(
        op.f("ix_event_vendor_subscriptions_event_vendor_id"), "event_vendor_subscriptions", ["event_vendor_id"]
    )
    op.create_index(op.f("ix_event_vendor_subscriptions_business_id"), "event_vendor_subscriptions", ["business_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_vendor_subscriptions_business_id"), table_name="event_vendor_subscriptions")
    op.drop_index(op.f("ix_event_vendor_subscriptions_event_vendor_id"), table_name="event_vendor_subscriptions")
    op.drop_table("event_vendor_subscriptions")
    op.drop_index(op.f("uq_event_vendors_event_id_stall_code"), table_name="event_vendors")
    op.drop_index(op.f("ix_event_vendors_business_id"), table_name="event_vendors")
    op.drop_table("event_vendors")
    op.drop_table("event_days")
    op.drop_table("events")
    op.drop_constraint(op.f("ck_businesses_industry_known"), "businesses", type_="check")
    op.drop_column("businesses", "industry")
