"""Add POS split-payment allocations + WhatsApp pay-by-link intents

Revision ID: 0055_pos_payment_allocations
Revises: 0054_delivery_reconciliation
Create Date: 2026-10-10

Two new tables plus one additive column on `orders`, purely additive,
fully reversible — nothing here rewrites or backfills an existing row.
See docs/SPLIT_PAYMENTS_CONTRACT.md for the full design and the exact
reasoning for every choice below; this docstring is the condensed version.

`pos_payment_link_intents` is created FIRST (same "every FK always targets
an already-existing table" ordering discipline migration 0040's own
docstring established) because `pos_payment_allocations.payment_link_intent_id`
references it.

WHY TWO TABLES, NOT ONE
------------------------
`pos_payment_allocations` is FINAL, CONFIRMED money — a row is only ever
inserted once a payment has actually settled (cash handed over, card
terminal approved, or a WhatsApp pay-by-link provider webhook verified the
exact amount), and it is never updated after insert. `pos_payment_link_intents`
is the asynchronous, stateful lead-up to a pay-by-link allocation
(PENDING -> PAID -> CONSUMED | FAILED | EXPIRED | CANCELLED) — a customer
may take anywhere from seconds to never to pay a link sent to their phone,
and that waiting period has no equivalent for cash/card. Forcing that
state machine into the immutable-allocation model would corrupt both
concepts at once.

UNIFIED ALLOCATION DESIGN
--------------------------
Every POS order created after this migration gets >= 1 allocation row,
including an ordinary single-method sale — one query path for till
cash-up / payment-mix analytics / receipts going forward, not two.
`Order.payment_method`/`cash_tendered_cents`/`change_due_cents`/
`payment_reference` are UNCHANGED for a single-method sale (every existing
reader keeps working exactly as today); for a genuinely split sale,
`Order.payment_method` becomes the summary marker "SPLIT" and those
per-order fields are left null. Orders from BEFORE this migration have
ZERO allocation rows — deliberately not backfilled (there is no reliable
way to retroactively decide a "SPLIT" marker never existed before this
feature did) — every reader must treat "zero allocation rows" as the
legacy pre-split-payments shape and fall back to the order's own columns,
exactly as it already does today.

`orders.refund_cash_cents` is a new nullable column recording how much of
an order's refund/void was handed back as physical cash, for till
reconciliation — distinct from the pre-existing `refund_amount_cents`
(the total refund, any method). See shared/models/order.py's own comment
for exactly when this is auto-computed vs. requires an explicit value.

Verified (this session) against the dedicated test environment described
in TESTING.md (`docker-compose.test.yml`, TEST_DATABASE_URL): full chain
0 -> head with this migration applied, downgrade back to
0054_delivery_reconciliation, and re-upgrade, all without error.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0055_pos_payment_allocations"
down_revision = "0054_delivery_reconciliation"
branch_labels = None
depends_on = None

_INTENTS = "pos_payment_link_intents"
_ALLOCATIONS = "pos_payment_allocations"


def upgrade() -> None:
    # ── pos_payment_link_intents ───────────────────────────────────────────
    op.create_table(
        _INTENTS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "till_session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("till_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="ZAR"),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("payment_link_url", sa.String(1024), nullable=True),
        sa.Column("customer_phone", sa.String(32), nullable=True),
        sa.Column("whatsapp_status", sa.String(24), nullable=True),
        sa.Column("whatsapp_message_id", sa.String(255), nullable=True),
        sa.Column("whatsapp_failure_reason", sa.String(255), nullable=True),
        sa.Column(
            "order_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_cents > 0", name="ck_pos_payment_link_intents_amount_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING','PAID','FAILED','EXPIRED','CANCELLED','CONSUMED')",
            name="ck_pos_payment_link_intents_status",
        ),
        sa.CheckConstraint("provider IN ('YOCO','PAYFAST')", name="ck_pos_payment_link_intents_provider"),
        sa.CheckConstraint(
            "status != 'CONSUMED' OR order_id IS NOT NULL",
            name="ck_pos_payment_link_intents_consumed_has_order",
        ),
    )
    op.create_index(f"ix_{_INTENTS}_business_id", _INTENTS, ["business_id"])
    op.create_index(f"ix_{_INTENTS}_till_session_id", _INTENTS, ["till_session_id"])
    op.create_index(f"ix_{_INTENTS}_order_id", _INTENTS, ["order_id"])
    op.create_index(f"ix_{_INTENTS}_business_status", _INTENTS, ["business_id", "status"])
    # THE "a paid intent is consumed at most once, ever" trust-boundary
    # constraint — a partial unique index (not a plain UniqueConstraint on a
    # nullable column, which Postgres would allow multiple NULLs through
    # anyway) so this is enforced even under concurrent checkout retries,
    # not just by the application's own row-lock discipline.
    op.create_index(
        f"uq_{_INTENTS}_order_id",
        _INTENTS, ["order_id"],
        unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL"),
    )

    # ── pos_payment_allocations ────────────────────────────────────────────
    op.create_table(
        _ALLOCATIONS,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "business_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "order_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "till_session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("till_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payment_method", sa.String(32), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("cash_tendered_cents", sa.Integer, nullable=True),
        sa.Column("change_due_cents", sa.Integer, nullable=True),
        sa.Column("payment_reference", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column(
            "payment_link_intent_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{_INTENTS}.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_pos_payment_allocations_amount_positive"),
        sa.CheckConstraint(
            "payment_method IN ('CASH','CARD','OTHER','PAY_BY_LINK')",
            name="ck_pos_payment_allocations_method",
        ),
        sa.CheckConstraint(
            "payment_method = 'CASH' OR (cash_tendered_cents IS NULL AND change_due_cents IS NULL)",
            name="ck_pos_payment_allocations_cash_fields_scoped",
        ),
        sa.CheckConstraint(
            "payment_method != 'CASH' OR "
            "(cash_tendered_cents IS NOT NULL AND cash_tendered_cents >= amount_cents)",
            name="ck_pos_payment_allocations_cash_covers_amount",
        ),
        sa.CheckConstraint(
            "payment_method = 'PAY_BY_LINK' OR "
            "(provider IS NULL AND payment_link_intent_id IS NULL)",
            name="ck_pos_payment_allocations_link_fields_scoped",
        ),
    )
    op.create_index(f"ix_{_ALLOCATIONS}_business_id", _ALLOCATIONS, ["business_id"])
    op.create_index(f"ix_{_ALLOCATIONS}_order_id", _ALLOCATIONS, ["order_id"])
    op.create_index(f"ix_{_ALLOCATIONS}_till_session_id", _ALLOCATIONS, ["till_session_id"])
    op.create_index(f"ix_{_ALLOCATIONS}_payment_link_intent_id", _ALLOCATIONS, ["payment_link_intent_id"])
    # The one composite index close_till's cash-aggregation query actually
    # drives: "sum CASH allocations for this till session."
    op.create_index(
        f"ix_{_ALLOCATIONS}_till_method", _ALLOCATIONS, ["till_session_id", "payment_method"],
    )

    # ── orders.refund_cash_cents ────────────────────────────────────────────
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_cash_cents INTEGER NULL"
    )

    # ── businesses: per-business Pay-by-Link WhatsApp template override ────
    # Same pattern as the pre-existing whatsapp_receipt_template_name/
    # _language pair — a platform default with an optional per-business
    # override, see shared/models/business.py.
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "whatsapp_payment_link_template_name VARCHAR(512) NULL"
    )
    op.execute(
        "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS "
        "whatsapp_payment_link_template_language VARCHAR(16) NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_payment_link_template_language")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_payment_link_template_name")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS refund_cash_cents")

    op.drop_index(f"ix_{_ALLOCATIONS}_till_method", table_name=_ALLOCATIONS)
    op.drop_index(f"ix_{_ALLOCATIONS}_payment_link_intent_id", table_name=_ALLOCATIONS)
    op.drop_index(f"ix_{_ALLOCATIONS}_till_session_id", table_name=_ALLOCATIONS)
    op.drop_index(f"ix_{_ALLOCATIONS}_order_id", table_name=_ALLOCATIONS)
    op.drop_index(f"ix_{_ALLOCATIONS}_business_id", table_name=_ALLOCATIONS)
    op.drop_table(_ALLOCATIONS)

    op.drop_index(f"uq_{_INTENTS}_order_id", table_name=_INTENTS)
    op.drop_index(f"ix_{_INTENTS}_business_status", table_name=_INTENTS)
    op.drop_index(f"ix_{_INTENTS}_order_id", table_name=_INTENTS)
    op.drop_index(f"ix_{_INTENTS}_till_session_id", table_name=_INTENTS)
    op.drop_index(f"ix_{_INTENTS}_business_id", table_name=_INTENTS)
    op.drop_table(_INTENTS)
