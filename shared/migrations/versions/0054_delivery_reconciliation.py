"""Add delivery_reconciliation_enabled to businesses — separate commercial
entitlement for CSV/manual delivery reconciliation, distinct from the live
delivery-platform marketplace entitlement.

Revision ID: 0054_delivery_reconciliation
Revises: 0053_restaurant_branding
Create Date: 2026-10-10

Named `0054_delivery_reconciliation` rather than the more descriptive
`0054_delivery_reconciliation_entitlement` because `alembic_version.version_num`
is `VARCHAR(32)` (see `shared/models/base.py`'s Alembic bootstrap) and the
longer id does not fit — confirmed by hitting
`StringDataRightTruncationError` against the real local test Postgres
container while developing this migration, rolled back cleanly (transactional
DDL), then fixed by shortening the id before re-running.

`delivery_platforms_enabled` (migration 0044) means commercial access to the
live delivery-platform marketplace integrations (Uber Eats direct order
injection, Mr D connection) — see docs/DELIVERY_PLATFORM_ARCHITECTURE.md.
The CSV/manual delivery-order reconciliation feature (ExternalOrdersPage /
routes_external_orders.py) is a separate, older, already-live piece of
baseline functionality that has never been gated by any entitlement column
at all — every tenant has always had it. UI V2.1 surfaced this as a real
gap: there was no way for NextGen to sell (or withhold) CSV reconciliation
independently of the marketplace entitlement, and the frontend's "Delivery
Orders" nav item could not correctly express "reconciliation only" vs
"marketplace only" vs "both" vs "neither" without conflating the two.

Purely additive, one NOT NULL boolean column with server_default false,
fully reversible — same shape as 0044_delivery_entitlements.py's three
columns and every other Commercial V1 entitlement gate
(inventory_enabled/digital_receipts_enabled/delivery_platforms_enabled/
advanced_analytics_enabled/loyalty_enabled).

Deliberately defaults FALSE for every existing tenant rather than backfilling
TRUE for current CSV-importer users, unlike 0051_loyalty_entitlement.py's
backfill: unlike Loyalty (which had a real self-serve on/off switch,
`loyalty_programs.is_enabled`, that already distinguished "using it" from
"not using it"), CSV reconciliation has never had any on/off state to read
back — there is no way to tell "a tenant who relies on this" from "a tenant
who has simply never needed to import a delivery CSV" from data alone. A
default-false rollout is a backend/model-only change with zero user-facing
effect until the frontend and admin-suite migrate to actually reading and
gating on it (tracked in TODO.md) — at that point Admin must explicitly grant
this entitlement to any tenant who currently relies on the CSV importer,
rather than this migration silently guessing who they are.

Verified (this session) against the dedicated test environment described in
TESTING.md (`docker-compose.test.yml`, TEST_DATABASE_URL): full chain
0 -> head with this migration applied, downgrade back to
0053_restaurant_branding, and re-upgrade, all without error.
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_delivery_reconciliation"
down_revision = "0053_restaurant_branding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "delivery_reconciliation_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS delivery_reconciliation_enabled")
