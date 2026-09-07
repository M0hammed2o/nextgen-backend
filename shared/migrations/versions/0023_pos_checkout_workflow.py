"""POS checkout workflow: cash tender/change, till linkage, idempotency,
table number, cashier tracking, grab-and-go flag, POS payment-method settings

Revision ID: 0023_pos_checkout_workflow
Revises: 0022_billing_invoices
Create Date: 2026-07-28

Restaurant POS redesign, Phase 2. The Phase 1 POS foundation (0017) built
order pricing/discount/till_sessions but never gave a POS sale a way to
actually record HOW it was paid (cash tendered/change, which till session
rang it up) or an idempotency guard against a double-tap checkout. This
migration adds exactly those columns plus a per-business POS payment-method
toggle set and a per-item "requires kitchen preparation" flag (grab-and-go).
Does not touch any existing column.
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_pos_checkout_workflow"
down_revision = "0022_billing_invoices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Orders: checkout/till/idempotency fields ─────────────────────────
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS table_number VARCHAR(32) NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS cash_tendered_cents INTEGER NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS change_due_cents INTEGER NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS till_session_id UUID NULL
                REFERENCES till_sessions(id) ON DELETE SET NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128) NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS created_by_user_id UUID NULL
    """)
    op.create_index(
        "ix_orders_till_session_id", "orders", ["till_session_id"]
    )
    # Idempotent replay lookup is always scoped to one business — a partial
    # unique index (NULLs excluded) lets non-POS orders keep a null key
    # without colliding with each other.
    op.create_index(
        "uq_orders_business_idempotency_key",
        "orders",
        ["business_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # ── Order items: removed-ingredient snapshot (immutable, like the
    #    existing options/add-on snapshots) ──────────────────────────────
    op.execute("""
        ALTER TABLE order_items
            ADD COLUMN IF NOT EXISTS removed_ingredients_snapshot JSONB NULL
    """)

    # ── Menu items: grab-and-go flag ──────────────────────────────────────
    op.execute("""
        ALTER TABLE menu_items
            ADD COLUMN IF NOT EXISTS requires_preparation BOOLEAN NOT NULL DEFAULT TRUE
    """)

    # ── Businesses: POS till payment-method settings ──────────────────────
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_cash_enabled BOOLEAN NOT NULL DEFAULT TRUE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_card_enabled BOOLEAN NOT NULL DEFAULT TRUE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_payfast_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_yoco_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_ikhoka_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_direct_eft_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_other_payment_enabled BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        ALTER TABLE businesses
            ADD COLUMN IF NOT EXISTS pos_table_number_required BOOLEAN NOT NULL DEFAULT FALSE
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_table_number_required")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_other_payment_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_direct_eft_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_ikhoka_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_yoco_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_payfast_enabled")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS pos_cash_enabled")
    op.execute("ALTER TABLE menu_items DROP COLUMN IF EXISTS requires_preparation")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS removed_ingredients_snapshot")
    op.drop_index("uq_orders_business_idempotency_key", table_name="orders")
    op.drop_index("ix_orders_till_session_id", table_name="orders")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS created_by_user_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS idempotency_key")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS till_session_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS change_due_cents")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS cash_tendered_cents")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS table_number")
