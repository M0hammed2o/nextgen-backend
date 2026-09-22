"""Add auto_accept_enabled to delivery_connections

Revision ID: 0046_delivery_auto_accept
Revises: 0045_delivery_status_unknown
Create Date: 2026-09-23

Design-gap fix found during the 2026-09-22 independent adversarial security
review's follow-up (KNOWN_BUGS.md — grouped with BUG-0087..BUG-0094; see
also docs/DELIVERY_PLATFORM_ARCHITECTURE.md's "Still NOT built" list, which
already documented this column's absence): `order_service.py::_handle_order`
previously auto-accepted EVERY fully-resolved delivery order straight into
the kitchen (Order created, stock deducted, accept_pos_order called) with no
staff confirmation step at all — there was no `auto_accept_enabled` column
anywhere in this codebase for a business to opt out of that. A restaurant
therefore had no way to require a human glance before a paid order commits
real ingredients and appears on the kitchen screen.

`auto_accept_enabled` defaults FALSE: a fully-mapped order now lands in
`READY_TO_ACCEPT` (an enum value that already existed and was already
reserved for exactly this — see canonical.py's DeliveryOrderStatus
docstring) and waits for an explicit staff accept via the existing
POST /business/delivery/orders/{order_id}/accept endpoint, rather than
auto-committing to the kitchen. A business can opt back into the original
(zero-touch) behaviour via the new
POST /business/delivery/connections/{provider}/auto-accept endpoint.

Purely additive, one NOT NULL boolean column with a server_default, fully
reversible — same profile as every other migration in this chain.
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_delivery_auto_accept"
down_revision = "0045_delivery_status_unknown"
branch_labels = None
depends_on = None

_TABLE = "delivery_connections"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE delivery_connections ADD COLUMN IF NOT EXISTS "
        "auto_accept_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE delivery_connections DROP COLUMN IF EXISTS auto_accept_enabled")
