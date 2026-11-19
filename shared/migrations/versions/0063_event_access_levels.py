"""Event preparation period + paid-reference uniqueness

Revision ID: 0063_event_access_levels
Revises: 0062_events_foundation
Create Date: 2026-11-18

Adds:
- events.prep_days INTEGER NOT NULL DEFAULT 14, CHECK 0..90: how many days
  before the first billable day a vendor may log in and configure.
- Partial unique index on event_vendor_subscriptions(payment_reference)
  WHERE status = 'PAID': one payment reference can settle only one paid
  subscription platform-wide.

Existing PAID rows sharing a reference would block the index; the upgrade
refuses with a clear message rather than touching payment data.
Downgrade drops the index, the check and the column (per-event prep periods
are lost; the default of 14 days then applies in code).
"""
import sqlalchemy as sa
from alembic import op

revision = "0063_event_access_levels"
down_revision = "0062_events_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("prep_days", sa.Integer(), nullable=False, server_default="14"))
    op.create_check_constraint(
        op.f("ck_events_prep_days_range"), "events", "prep_days >= 0 AND prep_days <= 90"
    )
    dupes = op.get_bind().execute(sa.text(
        "SELECT payment_reference FROM event_vendor_subscriptions "
        "WHERE status = 'PAID' AND payment_reference IS NOT NULL "
        "GROUP BY payment_reference HAVING count(*) > 1 LIMIT 1"
    )).first()
    if dupes:
        raise RuntimeError(
            "0063 refused: a payment_reference is already used by more than one PAID event "
            "subscription. Resolve it manually, then re-run."
        )
    op.create_index(
        op.f("uq_event_vendor_subscriptions_payment_reference_paid"),
        "event_vendor_subscriptions",
        ["payment_reference"],
        unique=True,
        postgresql_where=sa.text("status = 'PAID'"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("uq_event_vendor_subscriptions_payment_reference_paid"),
        table_name="event_vendor_subscriptions",
    )
    op.drop_constraint(op.f("ck_events_prep_days_range"), "events", type_="check")
    op.drop_column("events", "prep_days")
