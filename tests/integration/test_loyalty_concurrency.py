"""
Real-Postgres concurrency proof for BUG-0097 (KNOWN_BUGS.md): two genuinely
concurrent loyalty-reward redemptions against a balance sufficient for
exactly ONE must resolve to exactly one success and one refusal, and the
balance must never go negative.

Follows the same style/precedent as
tests/integration/test_wave0a_pos_concurrency.py — each attempt gets its
OWN AsyncSession/connection (an AsyncSession is not safe for concurrent use
from two coroutines at once) so the two genuinely race against Postgres's
own row-lock (SELECT ... FOR UPDATE), not against each other's Python state
in a single process.

Uses tests/integration/_db.py's get_session_factory() (TEST_DATABASE_URL —
see _env.py / KNOWN_BUGS.md BUG-0096), never backend.app.db.session's own
engine — see _db.py's own docstring for exactly why. Skips loudly (not
silently) if TEST_DATABASE_URL isn't set, via the fixtures in conftest.py.

Run against a disposable, positively-identified Postgres container, e.g.:

  docker run -d --name nextgen-loyalty-concurrency-testdb \\
    -e POSTGRES_USER=nextgen -e POSTGRES_PASSWORD=loyalty_local_only \\
    -e POSTGRES_DB=nextgen_loyalty_test -p 55930:5432 postgres:16-alpine
  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen:loyalty_local_only@localhost:55930/nextgen_loyalty_test"
  python -m alembic upgrade head
  python -m pytest tests/integration/test_loyalty_concurrency.py -q
"""
import asyncio
import uuid

import pytest

from backend.app.api.v1.routes_loyalty import RedeemRequest, redeem_loyalty_reward
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from tests.integration._db import get_session_factory


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


async def test_two_simultaneous_redemptions_of_a_balance_sufficient_for_exactly_one(
    test_business: Business, db,
):
    """
    Customer starts with exactly enough points for ONE redemption of a
    100-point reward (balance=100). Fire two concurrent redemption attempts
    of that SAME reward for that SAME customer. Before BUG-0097's fix, both
    could read balance=100, both pass the sufficiency check, and both
    write — the customer spends 100 points twice, and the balance
    (100 - 100 - 100 = -100) goes negative. After the fix: the second
    attempt blocks on the row lock until the first commits, then sees the
    already-decremented balance and is correctly refused.
    """
    business_id = test_business.id
    staff = _staff(business_id)

    # migration 0051, KNOWN_BUGS.md BUG-0107 — redeem_loyalty_reward now
    # requires Business.loyalty_enabled (checked first, before the program
    # lookup); test_business defaults it False like every other entitlement
    # flag, so entitle it explicitly, same as the LoyaltyProgram row below.
    test_business.loyalty_enabled = True
    db.add(test_business)

    program = LoyaltyProgram(business_id=business_id, is_enabled=True, min_redemption_points=0)
    reward = LoyaltyReward(business_id=business_id, name="Free Coffee", points_cost=100, is_active=True)
    customer = Customer(
        business_id=business_id, wa_id=None, phone_number="+27821112222",
        loyalty_points_balance=100, loyalty_tier="BRONZE",
    )
    db.add_all([program, reward, customer])
    await db.commit()
    await db.refresh(program)
    await db.refresh(reward)
    await db.refresh(customer)

    async def attempt():
        async with get_session_factory()() as session:
            body = RedeemRequest(customer_id=customer.id, reward_id=reward.id)
            try:
                result = await redeem_loyalty_reward(body=body, user=staff, db=session)
                return ("ok", result.balance_after)
            except AppError as exc:
                return ("refused", exc.code)
            finally:
                await session.rollback()

    results = await asyncio.gather(attempt(), attempt())
    outcomes = [r[0] for r in results]

    assert outcomes.count("ok") == 1, f"expected exactly one successful redemption, got: {results}"
    assert outcomes.count("refused") == 1, f"expected exactly one refusal, got: {results}"
    refused = next(r for r in results if r[0] == "refused")
    assert refused[1] == "INSUFFICIENT_POINTS", f"wrong refusal reason: {results}"

    ok = next(r for r in results if r[0] == "ok")
    assert ok[1] == 0, f"the successful redemption must leave the balance at exactly 0, got {ok[1]}"

    async with get_session_factory()() as verify_db:
        from sqlalchemy import select
        final = (
            await verify_db.execute(select(Customer).where(Customer.id == customer.id))
        ).scalar_one()
        assert final.loyalty_points_balance == 0, (
            f"balance must be exactly 0 after one successful redemption of a "
            f"100-point reward from a 100-point balance, got {final.loyalty_points_balance}"
        )
        assert final.loyalty_points_balance >= 0, "balance must never go negative (BUG-0097)"

        txns = (
            await verify_db.execute(
                select(LoyaltyTransaction).where(LoyaltyTransaction.customer_id == customer.id)
            )
        ).scalars().all()
        assert len(txns) == 1, f"exactly one REDEEM transaction must have been recorded, got {len(txns)}"
        assert txns[0].points_delta == -100

    async with get_session_factory()() as cleanup_db:
        from sqlalchemy import delete
        await cleanup_db.execute(delete(LoyaltyTransaction).where(LoyaltyTransaction.business_id == business_id))
        await cleanup_db.execute(delete(LoyaltyReward).where(LoyaltyReward.business_id == business_id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == business_id))
        await cleanup_db.execute(delete(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id))
        await cleanup_db.commit()
