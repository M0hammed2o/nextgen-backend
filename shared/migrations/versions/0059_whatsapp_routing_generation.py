"""Add businesses.whatsapp_routing_generation (fail-closed WhatsApp number handover)

Revision ID: 0059_whatsapp_routing_generation
Revises: 0058_business_menu_images
Create Date: 2026-10-29

Purely additive, fully reversible: one new NOT NULL column with a server
default, no existing column touched, no data backfill needed beyond the
default.

Closes the race identified during the Smokin Joes WhatsApp-number-handover
investigation (WORKLOG.md, 2026-10-29): `process_whatsapp_message`'s ARQ job
carried only the raw `phone_number_id`, so a message queued just before a
`whatsapp_phone_number_id` reassignment could be executed by the worker just
after it and get bound to the NEW owner instead of the one it was actually
sent to. This column is bumped every time a business's
`whatsapp_phone_number_id` changes (admin_api's `update_business`); the
webhook handler snapshots `(business_id, whatsapp_routing_generation)` into
the enqueued job at accept time, and the worker verifies the snapshot still
matches the business's CURRENT generation before processing — see
`backend/app/bot/pipeline.py::_process` and
`backend/app/api/v1/routes_webhook.py`.
"""
from alembic import op
import sqlalchemy as sa

revision = "0059_whatsapp_routing_generation"
down_revision = "0058_business_menu_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "whatsapp_routing_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("businesses", "whatsapp_routing_generation")
