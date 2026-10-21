"""POS/WhatsApp-only subscription entitlement + usage alerts

Revision ID: 0057_pos_entitlement
Revises: 0056_provider_refund_tracking
Create Date: 2026-10-20

KNOWN CHAIN COLLISION (flagged deliberately, not a mistake): an unmerged
sibling branch (`fix/whatsapp-receipt-text-fallback-demo`) also branched
off `0056_provider_refund_tracking` and independently claimed the
`0057_receipt_status_widen` revision id. Both branches cannot reach `main`
with `0057` used twice — whichever of the two merges SECOND must renumber
its migration to `0058_...` (and update its own `down_revision`) at that
time. Not resolved here per this task's explicit instruction; see
WORKLOG.md for this note recorded against both branches.

Three purely additive, fully reversible changes:

1. `businesses.pos_enabled` (NOT NULL, server_default true) — every
   existing tenant keeps today's behaviour with zero backfill needed.
   Gates every route in `routes_pos.py` except
   `GET /business/pos/settings` (see `backend/app/core/rbac.py::
   require_pos_enabled`, the one shared dependency for this check).

2. `businesses.whatsapp_order_allowance` (nullable, no default) — a
   WhatsApp-specific monthly order allowance, deliberately SEPARATE from
   the existing ALL-CHANNEL `monthly_order_quota`. NULL for every tenant
   until a SUPER_ADMIN explicitly sets one (e.g. for a "WhatsApp Only"
   package) — never silently reinterprets `monthly_order_quota`'s
   existing meaning for an already-combined POS+WhatsApp tenant.

3. `usage_alerts` — new table recording each (business, billing period,
   80/90/100% threshold) crossing exactly once, written only by
   `backend/app/worker.py::check_whatsapp_usage_alerts` (daily cron). See
   `shared/models/usage_alert.py` for why this is a new table rather than
   reusing `AuditEvent`.

Verify via a real downgrade/upgrade round-trip against the disposable
test Postgres (`docker-compose.test.yml`), querying the actual schema
after each step — this repo has a documented history of Alembic
constraint-naming surprises.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0057_pos_entitlement"
down_revision = "0056_provider_refund_tracking"
branch_labels = None
depends_on = None

_ALERTS = "usage_alerts"


def upgrade() -> None:
    # ── businesses.pos_enabled / whatsapp_order_allowance ──────────────────
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "pos_enabled BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "whatsapp_order_allowance INTEGER NULL"
    )

    # ── usage_alerts ────────────────────────────────────────────────────────
    op.create_table(
        _ALERTS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("billing_period_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("threshold_crossed", sa.Integer, nullable=False),
        sa.Column("orders_used", sa.Integer, nullable=False),
        sa.Column("orders_allowance", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # NOTE on the bare name below (no "ck_usage_alerts_" prefix): this is
        # KNOWN_BUGS.md BUG-0095 (found 2026-09-23, migrations 0042/0043,
        # still Open there) — op.create_table's CheckConstraint re-templates
        # ANY given `name=` through shared/models/base.py's naming
        # convention ("ck" -> "ck_%(table_name)s_%(constraint_name)s"), even
        # an already-fully-qualified name, producing a double-prefixed
        # constraint in the real database. Confirmed empirically against
        # THIS migration before fixing it (verified via `\d usage_alerts`
        # against the disposable test Postgres): passing the full
        # "ck_usage_alerts_threshold_crossed" string produced
        # "ck_usage_alerts_ck_usage_alerts_threshold_crossed" — the exact
        # same mechanism also visible, unfixed, on migration 0055's
        # pos_payment_link_intents table. Applying BUG-0095's own suggested
        # fix: pass only the short %(constraint_name)s token here ("threshold_
        # crossed") and let the convention supply the "ck_usage_alerts_"
        # prefix itself, rather than hardcoding the already-prefixed name as
        # every pre-existing migration in this chain did.
        sa.CheckConstraint(
            "threshold_crossed IN (80, 90, 100)",
            name="threshold_crossed",
        ),
        sa.UniqueConstraint(
            "business_id", "billing_period_started_at", "threshold_crossed",
            name="uq_usage_alerts_business_period_threshold",
        ),
    )
    op.create_index(f"ix_{_ALERTS}_business_id", _ALERTS, ["business_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{_ALERTS}_business_id", table_name=_ALERTS)
    op.drop_table(_ALERTS)
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_order_allowance")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_enabled")
