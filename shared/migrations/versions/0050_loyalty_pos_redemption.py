"""Loyalty reward redemption at POS checkout: reward cents value, order-level
redemption snapshot, and a ledger idempotency key.

Revision ID: 0050_loyalty_pos_redemption
Revises: 0049_loyalty_balance_nonneg
Create Date: 2026-09-23

Loyalty redemption already deducted points and wrote a ledger row, but never
reduced the sale total — handing over the reward was an out-of-band staff
action. This migration adds exactly what checkout-time redemption needs:

- `loyalty_rewards.value_cents` (NULLABLE): the reward's cents value at the
  till. NULL is deliberate, not a placeholder — every existing reward has no
  value today, and `NOT NULL DEFAULT 0` would silently turn every one of them
  into a real R0 till discount the moment this ships. NULL means "not
  redeemable at the till, hand it over manually" — the till rejects a
  NULL-valued reward with a distinct error code (REWARD_NOT_REDEEMABLE_AT_TILL)
  rather than discounting nothing. Deliberately no reward-type discriminator
  and no percentage support — single fixed cents only.

- Four columns on `orders`, all part of the immutable financial snapshot
  (never rewritten by a later refund/void, same reason `total_cents` isn't):
  `loyalty_discount_cents` (NOT NULL DEFAULT 0 — always a real number, same
  convention as `discount_cents`/`tax_cents`), `loyalty_reward_id` (FK ->
  loyalty_rewards, ON DELETE SET NULL — deleting a reward from the catalogue
  must never cascade into rewriting historical orders), `loyalty_points_redeemed`
  (NOT NULL DEFAULT 0 — the FULL points cost, even if the discount itself was
  clamped, so money and ledger agree on what happened), `loyalty_reward_name`
  (nullable snapshot — a reprint a month later must not depend on the FK
  resolving or the reward's current name, same pattern as `name_snapshot` on
  OrderItem).

- `loyalty_transactions.idempotency_key` + a partial unique index, following
  the exact pattern already established by
  `uq_stock_movements_business_id_idempotency_key` (migration 0025): NULLs
  excluded, scoped per business, so a transaction with no idempotency key
  (every pre-existing EARN/REDEEM/BONUS transaction, and the out-of-band
  manual redeem endpoint) never collides with anything. Used for both the
  original REDEEM transaction (`REDEEM:<order_id>`) and its reversal
  (`REDEEM_REVERSAL:<order_id>`) — belt-and-braces defense-in-depth on top of
  the order-row FOR UPDATE lock that void/refund already take, mirroring
  `reverse_sale_consumption`'s own idempotency-key philosophy exactly.

Uses raw `op.execute()` DDL throughout (not `op.create_check_constraint()`/
`sa.Column` autogenerate helpers for the partial index) to stay consistent
with the two established patterns in this chain (0023, 0025) and to sidestep
BUG-0095's double constraint-name-prefixing trap for anything that might
later gain a CHECK constraint here.
"""
from alembic import op

revision = "0050_loyalty_pos_redemption"
down_revision = "0049_loyalty_balance_nonneg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── loyalty_rewards: cents value for till redemption ───────────────────
    op.execute("""
        ALTER TABLE loyalty_rewards
            ADD COLUMN IF NOT EXISTS value_cents INTEGER NULL
    """)

    # ── orders: redemption snapshot (immutable once set) ───────────────────
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS loyalty_discount_cents INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS loyalty_reward_id UUID NULL
                REFERENCES loyalty_rewards(id) ON DELETE SET NULL
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS loyalty_points_redeemed INTEGER NOT NULL DEFAULT 0
    """)
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS loyalty_reward_name VARCHAR(255) NULL
    """)
    op.create_index(
        "ix_orders_loyalty_reward_id", "orders", ["loyalty_reward_id"]
    )

    # ── loyalty_transactions: ledger idempotency key ────────────────────────
    op.execute("""
        ALTER TABLE loyalty_transactions
            ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(160) NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_loyalty_transactions_business_id_idempotency_key
            ON loyalty_transactions (business_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_loyalty_transactions_business_id_idempotency_key")
    op.execute("ALTER TABLE loyalty_transactions DROP COLUMN IF EXISTS idempotency_key")

    op.drop_index("ix_orders_loyalty_reward_id", table_name="orders")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS loyalty_reward_name")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS loyalty_points_redeemed")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS loyalty_reward_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS loyalty_discount_cents")

    op.execute("ALTER TABLE loyalty_rewards DROP COLUMN IF EXISTS value_cents")
