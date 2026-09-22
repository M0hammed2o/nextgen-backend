"""Extend external_product_mappings for live delivery-API connections

Revision ID: 0043_delivery_mapping_extension
Revises: 0042_delivery_orders
Create Date: 2026-09-22

Fourth of five delivery-marketplace migrations. Extends the EXISTING
external_product_mappings table (used today by the CSV importer) rather
than creating a new table — see docs/DELIVERY_PLATFORM_ARCHITECTURE.md §3/§9
and DECISIONS.md DECISION-0030, and shared/models/external_orders.py's
updated module docstring.

THE CSV IMPORTER MUST KEEP WORKING IDENTICALLY — verified by design:
  - Every new column is nullable or has a server_default, so every existing
    row is valid with zero required backfill beyond `status` (set below).
  - The CSV importer's own unique constraint
    (uq_external_product_mappings_business_platform_kind_key) and its
    lookup path (external_orders/service.py::load_mappings, keyed on
    (business_id, platform, kind, external_key)) are completely untouched —
    neither reads nor writes any column added here.
  - The new partial unique index is keyed on `external_id`, a column the
    CSV path never populates (external_id stays NULL for every CSV-created
    row), so it can never collide with or constrain CSV behaviour.

Data migration: existing rows get status='MAPPED' (a saved mapping row
already means something was resolved) except rows with is_ignored=true,
which get status='IGNORED' — so the live-connection status vocabulary is
consistent from day one without silently mis-describing pre-existing CSV
mappings as 'UNMAPPED'.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_delivery_mapping_extension"
down_revision = "0042_delivery_orders"
branch_labels = None
depends_on = None

_TABLE = "external_product_mappings"
_OLD_CHECK = "ck_external_product_mappings_kind_known"
_NEW_KIND_VALUES = ("ITEM", "MODIFIER", "MODIFIER_GROUP", "CATEGORY")
_PARTIAL_UNIQUE_INDEX = "uq_external_product_mappings_business_platform_kind_external_id"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "external_id VARCHAR(128)"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "status VARCHAR(16) NOT NULL DEFAULT 'MAPPED'"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS status_reason TEXT"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS "
        "delivery_connection_id UUID REFERENCES delivery_connections(id) ON DELETE SET NULL"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS provider_snapshot_json JSONB"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ"
    )

    # Data migration: an already-ignored CSV mapping is 'IGNORED', not
    # 'MAPPED' — the server_default above only covers the common case.
    op.execute(
        f"UPDATE {_TABLE} SET status = 'IGNORED' WHERE is_ignored = TRUE"
    )

    # Widen the kind CHECK constraint to admit the two new live-connection
    # kinds. Dropped and recreated rather than altered in place — Postgres
    # has no ALTER CHECK CONSTRAINT.
    op.drop_constraint(_OLD_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _OLD_CHECK,
        _TABLE,
        "kind IN (" + ", ".join(f"'{v}'" for v in _NEW_KIND_VALUES) + ")",
    )

    op.create_check_constraint(
        "ck_external_product_mappings_status_known",
        _TABLE,
        "status IN ('MAPPED', 'UNMAPPED', 'CHANGED', 'INVALID', 'IGNORED')",
    )

    # Live-connection mappings key off the provider's STABLE id. Partial
    # because a CSV row's external_id is always NULL.
    op.create_index(
        _PARTIAL_UNIQUE_INDEX,
        _TABLE,
        ["business_id", "platform", "kind", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    # NOTE: this recreates the original 2-value CHECK constraint
    # unconditionally. If any row has been saved with kind='MODIFIER_GROUP'
    # or 'CATEGORY' by the time this runs, op.create_check_constraint below
    # will fail loudly (constraint violation) rather than silently truncating
    # data — an operator must resolve those rows first, same fail-loud
    # posture as migration 0037's duplicate-waba_id guard.
    op.drop_index(_PARTIAL_UNIQUE_INDEX, table_name=_TABLE)
    op.drop_constraint("ck_external_product_mappings_status_known", _TABLE, type_="check")
    op.drop_constraint(_OLD_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _OLD_CHECK,
        _TABLE,
        "kind IN ('ITEM', 'MODIFIER')",
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS last_seen_at")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS provider_snapshot_json")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS delivery_connection_id")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS status_reason")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS status")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS external_id")
