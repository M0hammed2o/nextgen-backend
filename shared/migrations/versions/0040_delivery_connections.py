"""Add delivery_connections table

Revision ID: 0040_delivery_connections
Revises: 0039_digital_receipts
Create Date: 2026-09-22

First of five migrations for the delivery-marketplace integration module
(see docs/DELIVERY_PLATFORM_ARCHITECTURE.md, DECISIONS.md DECISION-0030).
Purely additive, one new table, fully reversible.

`delivery_connections` is one row per (business, provider) and carries the
single most important constraint in the whole module: a PARTIAL UNIQUE index
on (provider, external_store_id) WHERE external_store_id IS NOT NULL. This
is what makes "one store id belongs to at most one business, platform-wide"
a database fact rather than an application-level assumption — the ONLY
permitted source of business_id for an inbound delivery webhook (see
backend/app/delivery/tenant_resolver.py). Modelled directly on migration
0037's WABA-uniqueness fix.

MIGRATION ORDERING NOTE (design doc §3 "ordering trap", resolved by
reordering rather than a two-step FK): the design flagged that
delivery_orders.raw_event_id FKs delivery_webhook_events.id, which doesn't
exist yet at delivery_orders' natural place in a connections-orders-events
migration order. Rather than create the column as a plain UUID in one
migration and add the FK constraint in a later one, this chain is simply
ordered connections (0040) -> webhook_events (0041) -> orders (0042), so
every FK in every migration always targets an already-existing table. No
in-place ALTER ... ADD CONSTRAINT step is needed anywhere in this chain.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0040_delivery_connections"
down_revision = "0039_digital_receipts"
branch_labels = None
depends_on = None

_TABLE = "delivery_connections"


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
        sa.Column("external_store_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("environment", sa.String(16), nullable=False, server_default="production"),
        sa.Column("provisioning_token_encrypted", sa.Text, nullable=True),
        sa.Column("store_status", sa.String(16), nullable=True),
        sa.Column("last_status_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_reason", sa.Text, nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "business_id", "provider",
            name="uq_delivery_connections_business_provider",
        ),
        sa.CheckConstraint(
            "provider IN ('UBER_EATS', 'MR_D')",
            name="ck_delivery_connections_provider_known",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'PAUSED', 'DISCONNECTED', 'ERROR')",
            name="ck_delivery_connections_status_known",
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'sandbox')",
            name="ck_delivery_connections_environment_known",
        ),
    )
    op.create_index(
        "ix_delivery_connections_business_id", _TABLE, ["business_id"]
    )
    # THE trust-boundary constraint — see module docstring above.
    op.create_index(
        "uq_delivery_connections_provider_external_store_id",
        _TABLE,
        ["provider", "external_store_id"],
        unique=True,
        postgresql_where=sa.text("external_store_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_delivery_connections_provider_external_store_id", table_name=_TABLE
    )
    op.drop_index("ix_delivery_connections_business_id", table_name=_TABLE)
    op.drop_table(_TABLE)
