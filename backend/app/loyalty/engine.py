"""
Loyalty engine — points earning, tier computation, reward redemption.

Automation this phase deliberately does NOT wire up (config exists, the
trigger doesn't — see KNOWN_BUGS.md/TODO.md): birthday rewards (needs a
daily scheduled job + a way to know a customer's birthday, which nothing
collects today) and referral rewards (needs a referral-code/tracking system
that doesn't exist). double_points_promo IS wired — it's just a date-range
check against existing config, no new infrastructure required.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.utils.phone import normalize_sa_mobile
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction


def redeem_idempotency_key(order_id: uuid.UUID) -> str:
    return f"REDEEM:{order_id}"


def redeem_reversal_idempotency_key(order_id: uuid.UUID) -> str:
    return f"REDEEM_REVERSAL:{order_id}"


def compute_tier(points_balance: int, program: LoyaltyProgram) -> str:
    if points_balance >= program.gold_tier_points:
        return "GOLD"
    if points_balance >= program.silver_tier_points:
        return "SILVER"
    return "BRONZE"


async def _lock_customer_for_balance_update(
    db: AsyncSession, business_id: uuid.UUID, customer_id: uuid.UUID,
) -> Customer:
    """
    Re-select and row-lock (SELECT ... FOR UPDATE) the customer row whose
    `loyalty_points_balance` is about to be read-modify-written, following
    the same locking pattern already established in routes_pos.py
    (`_get_open_till`, `_load_pos_order_for_reversal`,
    `_require_open_till_for_cash_reversal`).

    Without this, two concurrent mutations of the SAME customer's balance
    (e.g. two redemptions, or a redemption racing an order-paid earn) both
    read the same stale balance, both pass validation, and both write —
    a lost update. Today that just drifts the points number; it becomes a
    cash-loss bug the moment a redemption reduces a sale total (BUG-0097,
    KNOWN_BUGS.md). Callers must be inside a transaction that stays open
    until they commit — the lock is only held for the life of that
    transaction, same as every other FOR UPDATE caller in this codebase.

    `execution_options(populate_existing=True)` is NOT optional here, unlike
    routes_pos.py's till/order lock helpers: those are always the FIRST
    load of their row in the session, so there's nothing stale to refresh.
    Every caller of this function (redeem_loyalty_reward,
    award_loyalty_points_if_enabled) already did an earlier UNLOCKED select
    of the SAME customer row for its own 404 check before reaching here —
    without populate_existing, SQLAlchemy's identity map returns that same
    Python object with its ALREADY-loaded (pre-lock, potentially stale)
    loyalty_points_balance left untouched, because a plain re-select of an
    object already present in the identity map does not, by default,
    overwrite its already-loaded attributes. The FOR UPDATE lock would then
    be correctly acquired at the SQL level while every balance comparison
    and write in Python silently kept operating on stale data — i.e. a real
    lost update that a naive "just add .with_for_update()" fix would NOT
    have caught. Caught by this fix's OWN two-real-connection concurrency
    test (tests/integration/test_loyalty_concurrency.py) failing before
    this option was added: both concurrent redemptions read the stale
    pre-lock balance and both "succeeded", exactly the bug this whole
    change exists to close.
    """
    result = await db.execute(
        select(Customer)
        .where(Customer.id == customer_id, Customer.business_id == business_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked = result.scalar_one_or_none()
    if locked is None:
        raise NotFoundError("Customer", str(customer_id))
    return locked


def _is_double_points_active(program: LoyaltyProgram, now: datetime) -> bool:
    if not program.double_points_promo_active:
        return False
    if program.double_points_promo_starts_at and now < program.double_points_promo_starts_at:
        return False
    if program.double_points_promo_ends_at and now > program.double_points_promo_ends_at:
        return False
    return True


async def award_points_for_paid_order(
    db: AsyncSession, business_id: uuid.UUID, customer: Customer, order, program: LoyaltyProgram,
) -> int:
    """
    Award points for a paid order. Returns the number of points awarded (0 if
    the program is disabled or the order earns nothing). Mutates `customer`
    in place and adds a LoyaltyTransaction — caller is responsible for commit.

    Row-locks the customer before the read-modify-write (see
    `_lock_customer_for_balance_update`) — this is a read-modify-write of
    `loyalty_points_balance` exactly like `redeem_reward`, just in the
    opposite (additive) direction. Two orders for the same customer paid
    concurrently could otherwise both read the same stale balance and both
    write, losing one of the two awards. Not a cash-loss path (points are
    under/over-credited, not spent twice), but the same defect class, so it
    gets the same treatment rather than being left "probably fine."
    """
    if not program.is_enabled:
        return 0

    now = datetime.now(timezone.utc)
    base_points = (order.total_cents // 100) * program.points_per_rand + program.points_per_order
    multiplier = 2 if _is_double_points_active(program, now) else 1
    points = base_points * multiplier
    if points <= 0:
        return 0

    customer = await _lock_customer_for_balance_update(db, business_id, customer.id)
    customer.loyalty_points_balance += points
    customer.loyalty_tier = compute_tier(customer.loyalty_points_balance, program)
    if program.points_expiry_days:
        customer.loyalty_points_expire_at = now + timedelta(days=program.points_expiry_days)

    db.add(LoyaltyTransaction(
        business_id=business_id,
        customer_id=customer.id,
        order_id=getattr(order, "id", None),
        transaction_type="EARN",
        points_delta=points,
        balance_after=customer.loyalty_points_balance,
        reason=f"Order {getattr(order, 'order_number', '')}" + (" (2x promo)" if multiplier == 2 else ""),
    ))
    return points


async def award_loyalty_points_if_enabled(db: AsyncSession, business_id: uuid.UUID, order) -> None:
    """
    Convenience wrapper for the two payment-confirmation call sites
    (`routes_orders.py::update_payment_status`, `routes_payments.py::_mark_order_paid`)
    so neither has to duplicate "check enabled, load program, load customer, award."
    Silently does nothing if the order has no linked customer or loyalty isn't
    enabled — this must never be able to block a payment confirmation.
    """
    if not getattr(order, "customer_id", None):
        return
    try:
        program_result = await db.execute(select(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id))
        program = program_result.scalar_one_or_none()
        if not program or not program.is_enabled:
            return

        cust_result = await db.execute(select(Customer).where(Customer.id == order.customer_id))
        customer = cust_result.scalar_one_or_none()
        if not customer:
            return

        await award_points_for_paid_order(db, business_id, customer, order, program)
        await db.commit()
    except Exception:
        import logging
        logging.getLogger("nextgen.loyalty").exception(
            "Failed to award loyalty points for order %s — payment confirmation itself already succeeded",
            getattr(order, "id", "?"),
        )


async def redeem_reward(
    db: AsyncSession, business_id: uuid.UUID, customer: Customer, reward: LoyaltyReward, program: LoyaltyProgram,
    *, order_id: uuid.UUID | None = None,
) -> LoyaltyTransaction:
    """
    Redeem a reward for a customer. Raises AppError on any validation failure.

    Row-locks the customer (`SELECT ... FOR UPDATE`) before reading/mutating
    `loyalty_points_balance` — see `_lock_customer_for_balance_update`. This
    closes the lost-update race where two concurrent redemptions both read
    the same balance, both pass the sufficiency check, and both write,
    letting a customer spend the same points twice.

    `order_id` is optional — the out-of-band manual redeem endpoint
    (`routes_loyalty.py::redeem_loyalty_reward`) has none, matching
    LoyaltyTransaction.order_id's own "not every transaction is order-driven"
    nullability. When given (POS checkout, via `redeem_reward_for_checkout`),
    it stamps a deterministic `idempotency_key` on the written transaction —
    belt-and-braces defense on top of the order's own idempotency guard,
    mirroring `post_sale_consumption`'s ledger keys.
    """
    # Cheap in-memory checks first — no need to take the row lock at all if
    # the redemption is going to be rejected regardless of balance.
    if not program.is_enabled:
        raise AppError("LOYALTY_DISABLED", "Loyalty programme is not enabled for this business", 422)
    if not reward.is_active:
        raise AppError("REWARD_INACTIVE", "This reward is no longer available", 422)

    customer = await _lock_customer_for_balance_update(db, business_id, customer.id)

    if customer.loyalty_points_balance < program.min_redemption_points:
        raise AppError(
            "BELOW_MIN_REDEMPTION",
            f"A minimum of {program.min_redemption_points} points is required before any redemption",
            422,
        )
    if customer.loyalty_points_balance < reward.points_cost:
        raise AppError("INSUFFICIENT_POINTS", "Customer does not have enough points for this reward", 422)

    customer.loyalty_points_balance -= reward.points_cost
    customer.loyalty_tier = compute_tier(customer.loyalty_points_balance, program)

    txn = LoyaltyTransaction(
        business_id=business_id,
        customer_id=customer.id,
        order_id=order_id,
        transaction_type="REDEEM",
        points_delta=-reward.points_cost,
        balance_after=customer.loyalty_points_balance,
        reason=f"Redeemed: {reward.name}",
        idempotency_key=redeem_idempotency_key(order_id) if order_id else None,
    )
    db.add(txn)
    return txn


@dataclass
class CheckoutRedemption:
    """
    Result of redeeming a reward at POS checkout — see
    `redeem_reward_for_checkout`. `discount_cents` is the CLAMPED amount
    actually applied to the sale; `points_cost` is always the reward's FULL
    cost, deducted in full regardless of clamping, so the ledger and the
    money agree on what happened (see DECISIONS.md, "reward worth more than
    the sale").
    """

    txn: LoyaltyTransaction
    discount_cents: int
    points_cost: int
    shortfall_cents: int
    reward_name: str


async def redeem_reward_for_checkout(
    db: AsyncSession, business_id: uuid.UUID, customer: Customer, reward: LoyaltyReward, program: LoyaltyProgram,
    *, order_id: uuid.UUID, max_discountable_cents: int,
) -> CheckoutRedemption:
    """
    POS-checkout entry point for `redeem_reward()` — identical validation and
    row-locking, plus the "reward worth more than the sale" product decision:
    clamp the discount so the order total can never go below zero or owe
    change, while still deducting the reward's FULL points cost. The caller
    is expected to compute `max_discountable_cents` from the order's own
    subtotal (minus any staff discount already applied) BEFORE delivery fee
    and VAT — a reward discounts the food, not the delivery fee, and clamping
    against a food-only base means the eventual order total (subtotal -
    discount - loyalty_discount + delivery_fee) can never go negative either.

    Raises `AppError` (`REWARD_NOT_REDEEMABLE_AT_TILL`, 422) if the reward has
    no `value_cents` configured — such a reward exists for manual handover
    only (see LoyaltyReward.value_cents' own docstring) and must never
    silently apply a R0 discount.
    """
    if reward.value_cents is None:
        raise AppError(
            "REWARD_NOT_REDEEMABLE_AT_TILL",
            "This reward has no monetary value configured for till redemption — hand it over manually instead",
            422,
        )

    txn = await redeem_reward(db, business_id, customer, reward, program, order_id=order_id)

    discount_cents = min(int(reward.value_cents), max(0, int(max_discountable_cents)))
    shortfall_cents = int(reward.value_cents) - discount_cents

    return CheckoutRedemption(
        txn=txn,
        discount_cents=discount_cents,
        points_cost=reward.points_cost,
        shortfall_cents=shortfall_cents,
        reward_name=reward.name,
    )


async def reverse_redemption(
    db: AsyncSession, business_id: uuid.UUID, order_id: uuid.UUID, *, notes: str | None = None,
) -> int:
    """
    Return points spent on a redeemed reward back to the customer's balance —
    the loyalty half of a POS void/refund, mirroring
    `backend/app/inventory/ledger.py::reverse_sale_consumption`'s philosophy
    exactly: read the ORIGINAL LoyaltyTransaction row for this order rather
    than re-deriving anything (so a reward's points_cost changing after the
    sale can never produce a different reversal amount than what was
    actually charged), and be idempotent by construction — an order with no
    REDEEM transaction, or one already reversed, returns 0 and writes
    nothing, so a double-tapped void/refund cannot credit points twice.

    Callers (`routes_pos.py::void_pos_order`/`refund_pos_order`) already hold
    a `FOR UPDATE` lock on the ORDER row via `_load_pos_order_for_reversal`
    before this is ever called, which is the PRIMARY guard against a genuine
    double-tap (the second call sees payment_status already changed and is
    rejected before reaching here at all) — the idempotency-key check below
    is belt-and-braces on top of that, same layered-defense pattern as the
    stock ledger.

    Returns the number of points actually restored (0 if there was nothing
    to reverse).
    """
    original = (
        await db.execute(
            select(LoyaltyTransaction).where(
                LoyaltyTransaction.business_id == business_id,
                LoyaltyTransaction.order_id == order_id,
                LoyaltyTransaction.transaction_type == "REDEEM",
            )
        )
    ).scalar_one_or_none()
    if original is None:
        return 0

    already_reversed = (
        await db.execute(
            select(LoyaltyTransaction.id).where(
                LoyaltyTransaction.business_id == business_id,
                LoyaltyTransaction.idempotency_key == redeem_reversal_idempotency_key(order_id),
            )
        )
    ).scalar_one_or_none()
    if already_reversed is not None:
        return 0

    program_result = await db.execute(
        select(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id)
    )
    program = program_result.scalar_one_or_none()

    customer = await _lock_customer_for_balance_update(db, business_id, original.customer_id)
    points_back = -original.points_delta  # the original REDEEM delta is negative
    customer.loyalty_points_balance += points_back
    if program:
        customer.loyalty_tier = compute_tier(customer.loyalty_points_balance, program)

    db.add(LoyaltyTransaction(
        business_id=business_id,
        customer_id=customer.id,
        order_id=order_id,
        transaction_type="ADJUST",
        points_delta=points_back,
        balance_after=customer.loyalty_points_balance,
        reason=notes or f"Reward redemption reversed for order {order_id}",
        idempotency_key=redeem_reversal_idempotency_key(order_id),
    ))
    return points_back


async def get_or_create_customer_by_phone(
    db: AsyncSession, business_id: uuid.UUID, phone_number: str, display_name: str | None = None,
) -> Customer:
    """
    Look up a customer by phone number within a business, creating one if
    none exists — the POS's entry point into loyalty for walk-in customers
    who have no WhatsApp identity (Customer.wa_id is nullable since
    migration 0019 specifically to support this).

    The phone number is normalised through the same
    `backend.app.utils.phone.normalize_sa_mobile` helper the digital-receipt
    path already uses (`routes_pos.py`'s request_digital_receipt), so
    "0821234567" and "+27821234567" resolve to the SAME customer/points
    wallet instead of silently creating two (BUG-0100, KNOWN_BUGS.md).
    Raises AppError (422) rather than falling back to the raw string on an
    unparseable number — a loyalty identity keyed on a typo the platform
    never validated is worse than asking the till operator to re-enter it,
    and matches the existing convention at routes_pos.py's digital-receipt
    phone handling.

    NOTE: this does NOT retroactively fix any customer row created before
    this change with an un-normalised phone_number already stored — see
    KNOWN_BUGS.md BUG-0100 for what a full remediation of that pre-existing
    data would require (a decision deliberately left to a human, not made
    here).
    """
    normalized = normalize_sa_mobile(phone_number)
    if normalized is None:
        raise AppError(
            "INVALID_PHONE_NUMBER",
            "Enter a valid South African mobile number",
            422,
        )

    result = await db.execute(
        select(Customer).where(Customer.business_id == business_id, Customer.phone_number == normalized)
    )
    customer = result.scalar_one_or_none()
    if customer:
        return customer

    customer = Customer(business_id=business_id, wa_id=None, phone_number=normalized, display_name=display_name)
    db.add(customer)
    await db.flush()
    return customer
