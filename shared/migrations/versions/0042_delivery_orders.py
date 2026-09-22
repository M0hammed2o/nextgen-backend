"""Add delivery_orders table

Revision ID: 0042_delivery_orders
Revises: 0041_delivery_webhook_events
Create Date: 2026-09-22

Third of five delivery-marketplace migrations. Purely additive, fully
reversible.

`delivery_orders` is a strictly-scoped provider-side lifecycle and
reconciliation SIDECAR — not a second sales system. See
shared/models/delivery.py's module docstring and DECISIONS.md
DECISION-0030 for the full qualified-exception rationale to the standing
"an imported sale is a real Order, not a separate entity" rule. In short:
no line-item child table (line items live once as a JSONB intake snapshot
here, then once as real order_items once/if a canonical Order is created);
backend/app/analytics/revenue.py must never read this table; inventory
reads only the created Order.

Both FKs referenced here (delivery_connections, delivery_webhook_events)
already exist as of migrations 0040/0041 — see that migration's docstring
for why this chain is ordered this way instead of a two-step
create-then-ALTER-ADD-CONSTRAINT for raw_event_id.

Idempotency: UniqueConstraint(business_id, provider, external_order_id) is
BUSINESS-scoped, not global — the same Uber order id must remain
independently importable by a different restaurant, same rule as the
pre-existing uq_orders_business_source_external_id.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0042_delivery_orders"
down_revision = "0041_delivery_webhook_events"
branch_labels = None
depends_on = None

_TABLE = "delivery_orders"

_STATUS_VALUES = (
    "RECEIVED", "FETCHING", "NEEDS_MAPPING", "READY_TO_ACCEPT",
    "ACCEPTED", "DENIED", "FULFILLED", "CANCELLED", "FAILED",
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column(
            "delivery_connection_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("delivery_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("external_order_id", sa.String(128), nullable=False),
        sa.Column("external_store_id", sa.String(128), nullable=False),
        sa.Column(
            "raw_event_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("delivery_webhook_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="RECEIVED"),
        sa.Column(
            "order_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("accept_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deny_reason_code", sa.String(32), nullable=True),
        sa.Column("deny_reason_text", sa.Text, nullable=True),
        sa.Column("gross_cents", sa.Integer, nullable=True),
        sa.Column("fee_cents", sa.Integer, nullable=True),
        sa.Column("restaurant_net_cents", sa.Integer, nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="ZAR"),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_order_snapshot_json", postgresql.JSONB, nullable=True),
        sa.Column("unresolved_items_json", postgresql.JSONB, nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "business_id", "provider", "external_order_id",
            name="uq_delivery_orders_business_provider_external_order_id",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUS_VALUES) + ")",
            name="ck_delivery_orders_status_known",
        ),
    )
    op.create_index("ix_delivery_orders_business_id", _TABLE, ["business_id"])
    op.create_index("ix_delivery_orders_status", _TABLE, ["status"])
    op.create_index("ix_delivery_orders_accept_deadline_at", _TABLE, ["accept_deadline_at"])


def downgrade() -> None:
    op.drop_index("ix_delivery_orders_accept_deadline_at", table_name=_TABLE)
    op.drop_index("ix_delivery_orders_status", table_name=_TABLE)
    op.drop_index("ix_delivery_orders_business_id", table_name=_TABLE)
    op.drop_table(_TABLE)
