"""
POS payment allocations — the normalized, auditable financial source of
truth for how a till sale was actually settled.

Split payments (migration 0055): a single POS order can be settled by two or
more payment allocations (e.g. R150 Cash + R300 Card), each an immutable row
here. `amount_cents` is always the portion of the ORDER TOTAL this
allocation settles -- never the amount physically handed over by the
customer, which for a CASH allocation is `cash_tendered_cents` (always >=
`amount_cents`, with `change_due_cents` the difference).

UNIFIED DESIGN: every POS order created after this migration gets at least
one allocation row, including an ordinary single-method (CASH/CARD/OTHER)
sale -- not just split ones. This is deliberate: it gives till cash-up and
payment-mix analytics exactly ONE code path to query for any order going
forward, instead of two (legacy single-field columns vs. a new table) that
could silently drift apart. `Order.payment_method`/`cash_tendered_cents`/
`change_due_cents`/`payment_reference` are UNCHANGED and still populated for
a single-method sale (so every existing reader of those columns keeps
working exactly as before); for a genuinely split sale, `Order.payment_method`
becomes the summary marker "SPLIT" and those per-order fields are left null
-- this table is the only place the real breakdown lives.

Orders created BEFORE this migration shipped have ZERO allocation rows (no
backfill -- see this migration's own docstring for why). Every reader of
this table (till cash-up, analytics, receipts) must treat "an order with no
allocation rows" as the pre-split-payments legacy shape and fall back to the
order's own payment_method/total_cents columns, exactly as today.

WHATSAPP PAY BY LINK -- TWO DISTINCT MODELS, DELIBERATELY SEPARATE
-------------------------------------------------------------------
A Pay-by-Link allocation above represents CONFIRMED money — it is only ever
inserted once a provider has verified an exact-amount payment. Getting there
is asynchronous and stateful (a customer may take anywhere from seconds to
never to pay a WhatsApp-delivered link), which is a fundamentally different
shape than an immutable allocation row. `POSPaymentLinkIntent` below is that
separate, mutable, state-machine entity
(PENDING -> PAID -> CONSUMED | FAILED | EXPIRED | CANCELLED). An allocation
is never created for an unpaid intent, and a PAID intent is consumed exactly
once (DB-enforced via a unique partial index on order_id, and
concurrency-safe via a row lock at consumption time — see
routes_pos.py::create_pos_order's intent-consumption block).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import Base, UUIDPrimaryKeyMixin


class POSPaymentAllocation(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "pos_payment_allocations"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # SET NULL (not CASCADE): a till session being deleted — which never
    # happens in product, only in a disposable test DB — must not take a
    # financial settlement record with it. Nullable because a till-less
    # context is not expected in practice (checkout always requires an open
    # till) but the column must not be a hard dependency.
    till_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("till_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    payment_method: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="CASH | CARD | OTHER | PAY_BY_LINK. An individual allocation "
                "is always ONE concrete method; SPLIT is an order-level "
                "summary value (Order.payment_method) and never appears here.",
    )
    amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Portion of the order's total_cents settled by this "
                "allocation — never the amount physically tendered.",
    )
    cash_tendered_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="CASH allocations only.",
    )
    change_due_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="CASH allocations only — cash_tendered_cents - amount_cents.",
    )
    payment_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="CARD/OTHER: terminal/transaction reference. PAY_BY_LINK: "
                "the provider's own charge/transaction id (e.g. Yoco 'chr_xxx'), "
                "same field/meaning as Order.payment_reference uses today.",
    )
    # ── Pay by Link (migration 0055) ──────────────────────────────────────
    provider: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
        comment="PAY_BY_LINK only — YOCO | PAYFAST (the only two providers "
                "whose webhook can verify an exact amount — see "
                "docs/SPLIT_PAYMENTS_CONTRACT.md's support matrix).",
    )
    payment_link_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pos_payment_link_intents.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="PAY_BY_LINK only — the POSPaymentLinkIntent this allocation "
                "was created from, once it was confirmed PAID and consumed.",
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="PAY_BY_LINK only — when the provider actually confirmed "
                "payment (may be well before this row's own created_at, "
                "which is when final checkout consumed the paid intent).",
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Cashier who rang this allocation up — not FK-enforced, "
                "same pattern as Order.created_by_user_id/cancelled_by_user_id.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        comment="Immutable — a row here is never updated after insert, only "
                "ever read; no updated_at column is needed.",
    )

    order = relationship("Order", back_populates="payment_allocations")
    payment_link_intent = relationship("POSPaymentLinkIntent", back_populates="allocation")

    def __repr__(self) -> str:
        return f"<POSPaymentAllocation {self.payment_method} {self.amount_cents}c order={self.order_id}>"


_INTENT_STATUSES = "'PENDING','PAID','FAILED','EXPIRED','CANCELLED','CONSUMED'"


class POSPaymentLinkIntent(Base, UUIDPrimaryKeyMixin):
    """
    A WhatsApp-delivered payment-link request for a POS split-payment
    allocation, created and sent BEFORE the final order exists. See this
    module's own docstring for why this is a separate entity from
    POSPaymentAllocation, and docs/SPLIT_PAYMENTS_CONTRACT.md for the full
    state machine / webhook correlation design.

    `payment_link_url` is stored for audit/resend/troubleshooting ONLY — it
    must never be returned by any Staff-facing API response (the product
    requirement is WhatsApp delivery only, never a link the cashier could
    read out, copy, or open in a browser).
    """
    __tablename__ = "pos_payment_link_intents"

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_pos_payment_link_intents_amount_positive"),
        CheckConstraint(f"status IN ({_INTENT_STATUSES})", name="ck_pos_payment_link_intents_status"),
        CheckConstraint(
            "provider IN ('YOCO','PAYFAST')", name="ck_pos_payment_link_intents_provider",
        ),
        CheckConstraint(
            "status != 'CONSUMED' OR order_id IS NOT NULL",
            name="ck_pos_payment_link_intents_consumed_has_order",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    till_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("till_sessions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Cashier who requested this link — not FK-enforced, same "
                "pattern as Order.created_by_user_id.",
    )

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ZAR")
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False,
        comment="YOCO | PAYFAST only — the only two providers whose webhook "
                "can verify an exact amount (see support matrix in "
                "docs/SPLIT_PAYMENTS_CONTRACT.md); enforced by CHECK too.",
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    provider_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Provider's own charge/transaction id once PAID.",
    )
    payment_link_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        comment="Audit/resend/troubleshooting only — NEVER returned to the "
                "Staff client. See this class's own docstring.",
    )

    customer_phone: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="E.164-normalized SA mobile the link was sent to.",
    )
    whatsapp_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True,
        comment="NOT_SENT | SENT | FAILED | TEMPLATE_REQUIRED — see "
                "backend/app/payments/pos_link_whatsapp.py.",
    )
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp_failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # SET NULL rather than a hard dependency, same reasoning as every other
    # nullable FK in this file — but see the partial unique index in this
    # migration (business_id, order_id WHERE order_id IS NOT NULL) for the
    # real "a paid intent attaches to exactly one order, ever" guarantee.
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="created_at + a short TTL (15 min) — long enough for a "
                "customer to act, short enough a stale link isn't a "
                "lingering liability. Enforced in application code (expiry "
                "is a time comparison, not something Postgres evaluates on "
                "its own) — see routes_pos.py's intent-status resolver.",
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    allocation = relationship("POSPaymentAllocation", back_populates="payment_link_intent", uselist=False)

    def __repr__(self) -> str:
        return f"<POSPaymentLinkIntent {self.status} {self.amount_cents}c business={self.business_id}>"
