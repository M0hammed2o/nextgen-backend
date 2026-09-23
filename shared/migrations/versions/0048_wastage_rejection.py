"""Wastage entry rejection: explicit reject state + reversal support

Revision ID: 0048_wastage_rejection
Revises: 0047_advanced_analytics
Create Date: 2026-09-23

Closes the review-queue half of KNOWN_BUGS.md BUG-0083. WastageEntry already
had approved_by_user_id (an implicit "approved" state); this adds the
symmetric "rejected" state so a manager reviewing a pending over-threshold
STAFF entry can act on it two ways, not just one — and so the API can tell a
client "PENDING_APPROVAL" apart from "REJECTED" instead of both looking like
"not approved yet".

A rejection needs a reason (why the manager disagrees the loss should
stand, distinct from the original entry's own reason code) and a timestamp,
mirroring approved_by_user_id's existing shape.

NOTE ON VERIFICATION: this migration was authored and tested locally with
down_revision temporarily pointed at "0046_delivery_auto_accept" (0047 was
being created concurrently by another agent and did not yet exist on disk).
It is committed here pointed at "0047_advanced_analytics" as instructed;
re-verify the chain once 0047 lands.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0048_wastage_rejection"
down_revision = "0047_advanced_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wastage_entries",
        sa.Column("rejected_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "wastage_entries",
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "wastage_entries",
        sa.Column("rejection_reason", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("wastage_entries", "rejection_reason")
    op.drop_column("wastage_entries", "rejected_at")
    op.drop_column("wastage_entries", "rejected_by_user_id")
