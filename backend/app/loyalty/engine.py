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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction


def compute_tier(points_balance: int, program: LoyaltyProgram) -> str:
    if points_balance >= program.gold_tier_points:
        return "GOLD"
    if points_balance >= program.silver_tier_points:
        return "SILVER"
    return "BRONZE"


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
    """
    if not program.is_enabled:
        return 0

    now = datetime.now(timezone.utc)
    base_points = (order.total_cents // 100) * program.points_per_rand + program.points_per_order
    multiplier = 2 if _is_double_points_active(program, now) else 1
    points = base_points * multiplier
    if points <= 0:
        return 0

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
) -> LoyaltyTransaction:
    """Redeem a reward for a customer. Raises AppError on any validation failure."""
    if not program.is_enabled:
        raise AppError("LOYALTY_DISABLED", "Loyalty programme is not enabled for this business", 422)
    if not reward.is_active:
        raise AppError("REWARD_INACTIVE", "This reward is no longer available", 422)
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
        order_id=None,
        transaction_type="REDEEM",
        points_delta=-reward.points_cost,
        balance_after=customer.loyalty_points_balance,
        reason=f"Redeemed: {reward.name}",
    )
    db.add(txn)
    return txn


async def get_or_create_customer_by_phone(
    db: AsyncSession, business_id: uuid.UUID, phone_number: str, display_name: str | None = None,
) -> Customer:
    """
    Look up a customer by phone number within a business, creating one if
    none exists — the POS's entry point into loyalty for walk-in customers
    who have no WhatsApp identity (Customer.wa_id is nullable since
    migration 0019 specifically to support this).
    """
    result = await db.execute(
        select(Customer).where(Customer.business_id == business_id, Customer.phone_number == phone_number)
    )
    customer = result.scalar_one_or_none()
    if customer:
        return customer

    customer = Customer(business_id=business_id, wa_id=None, phone_number=phone_number, display_name=display_name)
    db.add(customer)
    await db.flush()
    return customer
