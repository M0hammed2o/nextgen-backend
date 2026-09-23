"""
Loyalty Programme — per-business configurable points/rewards, added 2026-07-24.

Deliberately opt-in per business (LoyaltyProgram.is_enabled defaults False) —
never mandatory, per the platform's own product requirement. Everything a
business can customise lives on LoyaltyProgram; LoyaltyReward is the
business's own reward catalogue; LoyaltyTransaction is an append-only ledger
so point balances are always reconstructable/auditable, not just a mutable
counter nobody can explain later.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LoyaltyProgram(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per business — the program's own configuration, all optional/customisable."""
    __tablename__ = "loyalty_programs"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Opt-in — a business must explicitly turn this on; never mandatory"
    )

    # ── Earning rules ─────────────────────────────────────────────────────
    points_per_rand: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False,
        comment="Points earned per whole Rand spent (order total_cents / 100 * this)"
    )
    points_per_order: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Flat bonus points awarded per qualifying order, regardless of spend"
    )
    points_expiry_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Points expire this many days after being earned; null = never expire"
    )

    # ── Redemption rules ──────────────────────────────────────────────────
    min_redemption_points: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
        comment="Minimum point balance required before any redemption is allowed"
    )

    # ── Tiers (thresholds are cumulative point balances, not per-period) ──
    silver_tier_points: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    gold_tier_points: Mapped[int] = mapped_column(Integer, default=2000, nullable=False)

    # ── Bonus programmes (config only — see KNOWN_BUGS.md/TODO.md for what's
    #    not yet wired to an actual trigger: birthday/referral sends need a
    #    scheduler + messaging integration this phase doesn't build) ───────
    birthday_reward_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referral_reward_points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    double_points_promo_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    double_points_promo_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    double_points_promo_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<LoyaltyProgram business={self.business_id} enabled={self.is_enabled}>"


class LoyaltyReward(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One entry in a business's own reward catalogue (e.g. 'Free Coffee — 200 pts')."""
    __tablename__ = "loyalty_rewards"

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    points_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Added migration 0050 (POS redemption). NULL is deliberate — every
    # reward created before this column existed has no value, and defaulting
    # to 0 would silently turn each one into a real R0 till discount. NULL
    # means "not redeemable at the till, hand it over manually" — checkout
    # rejects a NULL-valued reward with a distinct error code rather than
    # discounting nothing. Single fixed cents only, deliberately — no
    # reward-type discriminator, no percentage support.
    value_cents: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Cents discount applied at POS checkout when this reward is "
                 "redeemed. NULL = not redeemable at the till (manual handover only)."
    )

    def __repr__(self) -> str:
        return f"<LoyaltyReward {self.name} ({self.points_cost}pts)>"


class LoyaltyTransaction(Base, UUIDPrimaryKeyMixin):
    """
    Append-only ledger of every points change — never mutated after creation.
    A customer's loyalty_points_balance is a cached total; this table is the
    source of truth it's derived from, so a balance is always explainable.
    """
    __tablename__ = "loyalty_transactions"
    __table_args__ = (
        # Belt-and-braces idempotency guard, added migration 0050 alongside
        # POS redemption — mirrors uq_stock_movements_business_id_idempotency_key
        # exactly (StockMovement, migration 0025). Partial index (WHERE
        # idempotency_key IS NOT NULL) is created in the migration, not here,
        # same reason StockMovement's own comment gives: SQLAlchemy's plain
        # UniqueConstraint below documents the intended constraint for
        # metadata/autogenerate purposes, but the actual DB constraint is the
        # partial index the migration creates directly.
        UniqueConstraint(
            "business_id",
            "idempotency_key",
            name="uq_loyalty_transactions_business_id_idempotency_key",
        ),
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="Nullable — not every transaction is order-driven (e.g. a manual adjustment, a birthday bonus)"
    )
    transaction_type: Mapped[str] = mapped_column(
        String(16), nullable=False,
        comment="EARN / REDEEM / EXPIRE / BONUS / ADJUST"
    )
    points_delta: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Signed — positive for EARN/BONUS, negative for REDEEM/EXPIRE"
    )
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Added migration 0050. Only ever set for a POS-checkout-driven REDEEM
    # (`REDEEM:<order_id>`) or its reversal (`REDEEM_REVERSAL:<order_id>`) —
    # every pre-existing transaction type, and the out-of-band manual
    # redeem endpoint, leaves this NULL, which the partial unique index
    # excludes from the uniqueness check entirely.
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<LoyaltyTransaction {self.transaction_type} {self.points_delta:+d}>"
