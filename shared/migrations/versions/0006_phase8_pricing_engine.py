"""Phase 8: Pricing engine — add-on tables + order_items breakdown columns

Revision ID: 0006_phase8_pricing_engine
Revises: 0005_unique_conversation_session
Create Date: 2026-07-02

Changes:
  - Creates menu_add_ons table (priced add-ons with quantity limits)
  - Creates menu_item_add_ons join table (which add-ons are available on which items)
  - Adds base_price_cents, option_adjustment_cents, add_on_total_cents,
    selected_options_snapshot, add_ons_snapshot to order_items
  - Backfills base_price_cents = unit_price_cents for existing rows (safe default)

All new order_items columns have DEFAULT 0 / are nullable, so existing rows are
fully backward compatible.

Written with IF NOT EXISTS / DO NOTHING guards so re-running is safe when the
database already has these objects from a previous partial attempt.
"""
from alembic import op


revision = "0006_phase8_pricing_engine"
down_revision = "0005_unique_conversation_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── menu_add_ons ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS menu_add_ons (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            business_id         UUID NOT NULL
                                REFERENCES businesses(id) ON DELETE CASCADE,
            name                VARCHAR(255) NOT NULL,
            price_cents         INTEGER NOT NULL,
            min_qty             INTEGER NOT NULL DEFAULT 0,
            max_qty             INTEGER NOT NULL DEFAULT 10,
            default_qty         INTEGER NOT NULL DEFAULT 1,
            inventory_item_id   UUID,
            is_active           BOOLEAN NOT NULL DEFAULT TRUE,
            is_deleted          BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order          INTEGER NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_menu_add_ons_business_id
        ON menu_add_ons (business_id)
    """)

    # ── menu_item_add_ons (join table) ────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS menu_item_add_ons (
            menu_item_id UUID NOT NULL
                REFERENCES menu_items(id) ON DELETE CASCADE,
            add_on_id    UUID NOT NULL
                REFERENCES menu_add_ons(id) ON DELETE CASCADE,
            PRIMARY KEY (menu_item_id, add_on_id)
        )
    """)

    # ── order_items: pricing breakdown columns ─────────────────────────────────
    # Each ADD COLUMN is guarded: the DO block skips the statement if the column
    # already exists, so re-running the migration is always safe.

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'order_items'
                AND column_name = 'base_price_cents'
            ) THEN
                ALTER TABLE order_items
                ADD COLUMN base_price_cents INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$
    """)

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'order_items'
                AND column_name = 'option_adjustment_cents'
            ) THEN
                ALTER TABLE order_items
                ADD COLUMN option_adjustment_cents INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$
    """)

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'order_items'
                AND column_name = 'add_on_total_cents'
            ) THEN
                ALTER TABLE order_items
                ADD COLUMN add_on_total_cents INTEGER NOT NULL DEFAULT 0;
            END IF;
        END $$
    """)

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'order_items'
                AND column_name = 'selected_options_snapshot'
            ) THEN
                ALTER TABLE order_items
                ADD COLUMN selected_options_snapshot JSONB;
            END IF;
        END $$
    """)

    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'order_items'
                AND column_name = 'add_ons_snapshot'
            ) THEN
                ALTER TABLE order_items
                ADD COLUMN add_ons_snapshot JSONB;
            END IF;
        END $$
    """)

    # Backfill: rows added before this migration have base_price_cents = 0
    # (from the column default), but the semantically correct value is
    # unit_price_cents. Only update rows where base_price_cents is still 0
    # and unit_price_cents is non-zero to avoid clobbering any already-correct rows.
    op.execute("""
        UPDATE order_items
        SET base_price_cents = unit_price_cents
        WHERE base_price_cents = 0
        AND unit_price_cents > 0
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS add_ons_snapshot")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS selected_options_snapshot")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS add_on_total_cents")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS option_adjustment_cents")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS base_price_cents")
    op.execute("DROP TABLE IF EXISTS menu_item_add_ons")
    op.execute("DROP INDEX IF EXISTS ix_menu_add_ons_business_id")
    op.execute("DROP TABLE IF EXISTS menu_add_ons")
