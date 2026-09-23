"""Add loyalty_enabled to businesses — platform-admin-only master-switch
entitlement gate for the Loyalty module, closing BUG-0107.

Revision ID: 0051_loyalty_entitlement
Revises: 0050_loyalty_pos_redemption
Create Date: 2026-09-23

Commercial gating gap (KNOWN_BUGS.md BUG-0107): every other sellable module
has a `Business`-level platform-admin-only entitlement flag —
`inventory_enabled`, `is_whatsapp_enabled`, `digital_receipts_enabled`,
`delivery_platforms_enabled`, `advanced_analytics_enabled`. Loyalty had none
— the only gate was `LoyaltyProgram.is_enabled`, a per-business config row
that any OWNER/MANAGER could flip to True themselves via
`PUT /business/loyalty/program`, with zero package-tier check anywhere.
`loyalty_enabled` closes that gap the same way the other five do: platform-
admin-writable (admin_api), business-plane READ-ONLY.

THIS IS A DATA-TRANSFORMING MIGRATION, NOT A PURELY ADDITIVE ONE — the first
in this release chain. A plain `ADD COLUMN ... DEFAULT FALSE` would silently
disable Loyalty, mid-release, for every tenant that already has it switched
on today (any business with an existing `loyalty_programs` row where
`is_enabled = true`) — an unacceptable regression for a paying customer who
is actively using the feature; commercially indistinguishable from NextGen
deciding to repossess a feature a customer is already using. Instead, this
migration backfills `loyalty_enabled = TRUE` for exactly those businesses
(and only those), so:
  - every tenant's CURRENT observable behaviour is preserved exactly — a
    business with an enabled programme today keeps working identically
    after this migration ships, with no support ticket, no re-enable step,
    and no silent feature loss;
  - every OTHER business (no `loyalty_programs` row at all, or one that
    exists but has `is_enabled = false`) defaults to `loyalty_enabled =
    FALSE`, exactly like every other Commercial V1 entitlement column when
    it was first introduced — the gap this migration exists to close is
    closed for them from this migration onward: they can no longer
    self-enable Loyalty without a platform admin granting the entitlement
    first, since `PUT /business/loyalty/program` is now additionally gated
    on this column (see backend/app/api/v1/routes_loyalty.py).

Deliberately does NOT infer entitlement from `is_enabled = false` rows —
a business that once tried Loyalty and turned it back off is not thereby
"grandfathered" into a permanent entitlement it never commercially bought;
only a CURRENTLY-enabled programme counts as "already using the feature
today," which is the one case this migration exists to protect.

`downgrade()` simply drops the column — the backfill needs no inverse to
write, since the column holding its result ceases to exist.

Verified (this session) against the dedicated test environment described in
TESTING.md (`docker-compose.test.yml`, TEST_DATABASE_URL) — full chain
0 -> head, and a targeted backfill proof: a business with an existing
enabled `loyalty_programs` row ends up `loyalty_enabled = TRUE`; a business
with no programme row, and a separate business with a disabled programme
row, both end up `loyalty_enabled = FALSE`.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_loyalty_entitlement"
down_revision = "0050_loyalty_pos_redemption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "loyalty_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Data-aware backfill: preserve current behaviour for every tenant
    # already using Loyalty today (an existing, currently-enabled
    # loyalty_programs row) — see this migration's own docstring above for
    # why a plain additive DEFAULT FALSE was not acceptable here.
    op.execute(
        "UPDATE businesses SET loyalty_enabled = TRUE "
        "WHERE id IN (SELECT business_id FROM loyalty_programs WHERE is_enabled = TRUE)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS loyalty_enabled")
