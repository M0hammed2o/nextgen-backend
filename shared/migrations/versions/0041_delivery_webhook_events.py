"""Add delivery_webhook_events table

Revision ID: 0041_delivery_webhook_events
Revises: 0040_delivery_connections
Create Date: 2026-09-22

Second of five delivery-marketplace migrations. Purely additive, fully
reversible. See 0040_delivery_connections.py for the ordering-trap note this
chain resolves by sequencing (connections -> webhook_events -> orders).

Raw inbound webhook event log with a defined two-stage retention policy
(`payload_redacted_at` at 30 days, row deletion at 400 — see
backend/app/delivery/intake.py). `business_id` is nullable and FK
ON DELETE SET NULL: an unknown-store event is logged (never refused
outright — we have no right to deny an order for a restaurant that isn't
our customer) with business_id NULL, and if a business is later deleted its
historical webhook log rows survive with business_id cleared rather than
being cascaded away.

Idempotency: UniqueConstraint(provider, event_id) is the event-log half of
the two-level "INSERT ... ON CONFLICT DO NOTHING" duplicate-webhook
protection described in the design doc §3.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0041_delivery_webhook_events"
down_revision = "0040_delivery_connections"
branch_labels = None
depends_on = None

_TABLE = "delivery_webhook_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("external_store_id", sa.String(128), nullable=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(64), nullable=True),
        sa.Column("environment", sa.String(16), nullable=True),
        sa.Column("signature_valid", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error_category", sa.String(32), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB, nullable=True),
        sa.Column("payload_redacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider", "event_id",
            name="uq_delivery_webhook_events_provider_event_id",
        ),
    )
    op.create_index(
        "ix_delivery_webhook_events_business_id", _TABLE, ["business_id"]
    )
    op.create_index(
        "ix_delivery_webhook_events_received_at", _TABLE, ["received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_webhook_events_received_at", table_name=_TABLE)
    op.drop_index("ix_delivery_webhook_events_business_id", table_name=_TABLE)
    op.drop_table(_TABLE)
