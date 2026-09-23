"""
Loyalty engine (backend/app/loyalty/engine.py) — points earning, tier
computation, and reward redemption. Loyalty is opt-in per business
(LoyaltyProgram.is_enabled defaults False) — every earning/redemption path
must be a safe no-op or a clear error when disabled, never a silent award.
"""
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.errors import AppError
from backend.app.loyalty.engine import (
    award_points_for_paid_order,
    compute_tier,
    get_or_create_customer_by_phone,
    redeem_reward,
)


def make_program(**overrides):
    p = types.SimpleNamespace(
        is_enabled=True, points_per_rand=1, points_per_order=0,
        points_expiry_days=None, min_redemption_points=0,
        silver_tier_points=500, gold_tier_points=2000,
        double_points_promo_active=False,
        double_points_promo_starts_at=None, double_points_promo_ends_at=None,
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def make_customer(**overrides):
    c = types.SimpleNamespace(
        id=uuid.uuid4(), loyalty_points_balance=0, loyalty_tier="BRONZE",
        loyalty_points_expire_at=None,
    )
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def make_order(**overrides):
    o = types.SimpleNamespace(id=uuid.uuid4(), order_number="BO-000001", total_cents=10000)
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


class FakeLockResult:
    """Stands in for the AsyncSession.execute() result of the new
    `_lock_customer_for_balance_update` SELECT ... FOR UPDATE reselect —
    returns whichever customer FakeDB was told to hand back, so these
    mocked-DB unit tests keep exercising the same business logic/assertions
    as before the locking fix, without a real Postgres."""

    def __init__(self, customer):
        self._customer = customer

    def scalar_one_or_none(self):
        return self._customer


class FakeDB:
    def __init__(self, customer=None):
        self.added = []
        self._customer = customer

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        # award_points_for_paid_order/redeem_reward both re-select the
        # customer under lock by (id, business_id) before mutating its
        # balance — this fake has no real query engine, so it just hands
        # back whatever customer object it was constructed with (the SAME
        # object the test already holds a reference to, matching how
        # SQLAlchemy's identity map would behave against a real session).
        return FakeLockResult(self._customer)


# ── Tier computation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("points,expected", [(0, "BRONZE"), (499, "BRONZE"), (500, "SILVER"), (1999, "SILVER"), (2000, "GOLD"), (5000, "GOLD")])
def test_compute_tier_thresholds(points, expected):
    assert compute_tier(points, make_program()) == expected


# ── Points earning ───────────────────────────────────────────────────────────

async def test_disabled_program_awards_nothing():
    program = make_program(is_enabled=False)
    customer = make_customer()
    db = FakeDB()
    points = await award_points_for_paid_order(db, uuid.uuid4(), customer, make_order(), program)
    assert points == 0
    assert customer.loyalty_points_balance == 0
    assert db.added == []


async def test_points_per_rand_and_per_order_combine():
    program = make_program(points_per_rand=2, points_per_order=10)
    customer = make_customer()
    db = FakeDB(customer=customer)
    order = make_order(total_cents=10000)  # R100
    points = await award_points_for_paid_order(db, uuid.uuid4(), customer, order, program)
    assert points == 100 * 2 + 10  # 210
    assert customer.loyalty_points_balance == 210
    assert len(db.added) == 1
    assert db.added[0].transaction_type == "EARN"


async def test_double_points_promo_doubles_award():
    now = datetime.now(timezone.utc)
    program = make_program(
        points_per_rand=1, double_points_promo_active=True,
        double_points_promo_starts_at=now - timedelta(hours=1),
        double_points_promo_ends_at=now + timedelta(hours=1),
    )
    customer = make_customer()
    db = FakeDB(customer=customer)
    points = await award_points_for_paid_order(db, uuid.uuid4(), customer, make_order(total_cents=10000), program)
    assert points == 200  # 100 base * 2x


async def test_double_points_promo_outside_date_range_not_applied():
    now = datetime.now(timezone.utc)
    program = make_program(
        points_per_rand=1, double_points_promo_active=True,
        double_points_promo_starts_at=now - timedelta(days=10),
        double_points_promo_ends_at=now - timedelta(days=1),  # already ended
    )
    customer = make_customer()
    db = FakeDB(customer=customer)
    points = await award_points_for_paid_order(db, uuid.uuid4(), customer, make_order(total_cents=10000), program)
    assert points == 100  # not doubled


async def test_tier_upgrades_after_earning_enough_points():
    program = make_program(points_per_rand=1, silver_tier_points=500)
    customer = make_customer(loyalty_points_balance=450)
    db = FakeDB(customer=customer)
    await award_points_for_paid_order(db, uuid.uuid4(), customer, make_order(total_cents=10000), program)
    assert customer.loyalty_points_balance == 550
    assert customer.loyalty_tier == "SILVER"


async def test_points_expiry_set_when_configured():
    program = make_program(points_expiry_days=30)
    customer = make_customer()
    db = FakeDB(customer=customer)
    await award_points_for_paid_order(db, uuid.uuid4(), customer, make_order(total_cents=10000), program)
    assert customer.loyalty_points_expire_at is not None


# ── Redemption ───────────────────────────────────────────────────────────────

def make_reward(**overrides):
    r = types.SimpleNamespace(id=uuid.uuid4(), name="Free Coffee", points_cost=100, is_active=True)
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


async def test_redeem_reward_success():
    program = make_program()
    customer = make_customer(loyalty_points_balance=500)
    reward = make_reward(points_cost=200)
    db = FakeDB(customer=customer)
    txn = await redeem_reward(db, uuid.uuid4(), customer, reward, program)
    assert customer.loyalty_points_balance == 300
    assert txn.points_delta == -200
    assert txn.balance_after == 300


async def test_redeem_reward_fails_when_disabled():
    program = make_program(is_enabled=False)
    customer = make_customer(loyalty_points_balance=500)
    with pytest.raises(AppError) as exc:
        await redeem_reward(FakeDB(), uuid.uuid4(), customer, make_reward(), program)
    assert exc.value.code == "LOYALTY_DISABLED"


async def test_redeem_reward_fails_when_inactive():
    program = make_program()
    customer = make_customer(loyalty_points_balance=500)
    with pytest.raises(AppError) as exc:
        await redeem_reward(FakeDB(), uuid.uuid4(), customer, make_reward(is_active=False), program)
    assert exc.value.code == "REWARD_INACTIVE"


async def test_redeem_reward_fails_below_minimum_redemption():
    program = make_program(min_redemption_points=1000)
    customer = make_customer(loyalty_points_balance=500)
    with pytest.raises(AppError) as exc:
        await redeem_reward(FakeDB(customer=customer), uuid.uuid4(), customer, make_reward(points_cost=100), program)
    assert exc.value.code == "BELOW_MIN_REDEMPTION"


async def test_redeem_reward_fails_insufficient_points():
    program = make_program()
    customer = make_customer(loyalty_points_balance=50)
    with pytest.raises(AppError) as exc:
        await redeem_reward(FakeDB(customer=customer), uuid.uuid4(), customer, make_reward(points_cost=100), program)
    assert exc.value.code == "INSUFFICIENT_POINTS"


async def test_redeem_reward_raises_not_found_if_customer_vanished_under_lock():
    """
    Defensive path: if the row-lock reselect finds no customer (e.g. deleted
    between the caller's initial load and redeem_reward's own lock), this
    must raise a clear NotFoundError rather than crash on a None balance
    comparison.
    """
    program = make_program()
    customer = make_customer(loyalty_points_balance=500)
    db = FakeDB(customer=None)  # simulates "vanished under lock"
    with pytest.raises(AppError) as exc:
        await redeem_reward(db, uuid.uuid4(), customer, make_reward(points_cost=100), program)
    assert exc.value.code == "CUSTOMER_NOT_FOUND"


# ── get_or_create_customer_by_phone ──────────────────────────────────────────

class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeCustomerDB:
    def __init__(self, existing=None):
        self._existing = existing
        self.added = []
        self.flushed = 0

    async def execute(self, stmt):
        return FakeResult(scalar=self._existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


async def test_get_or_create_customer_returns_existing():
    business_id = uuid.uuid4()
    existing = make_customer()
    db = FakeCustomerDB(existing=existing)
    result = await get_or_create_customer_by_phone(db, business_id, "0821234567")
    assert result is existing
    assert db.added == []


async def test_get_or_create_customer_creates_new_when_none_found():
    business_id = uuid.uuid4()
    db = FakeCustomerDB(existing=None)
    result = await get_or_create_customer_by_phone(db, business_id, "0821234567", "Jane")
    assert len(db.added) == 1
    assert db.added[0].wa_id is None
    # BUG-0100 fix: stored in normalised +27... form, not the raw local
    # "082..." shape the caller typed — this is what lets "0821234567" and
    # "+27821234567" resolve to the same customer instead of two.
    assert db.added[0].phone_number == "+27821234567"
    assert db.flushed == 1


async def test_get_or_create_customer_normalises_before_lookup():
    """
    A local "0821234567" and an international "+27821234567" must look up
    (and thus resolve to) the same stored customer — BUG-0100: previously a
    raw string comparison, so these created two separate points wallets for
    the same real person.
    """
    business_id = uuid.uuid4()
    existing = make_customer()
    db = FakeCustomerDB(existing=existing)
    result = await get_or_create_customer_by_phone(db, business_id, "+27 82 123 4567")
    assert result is existing
    assert db.added == []


async def test_get_or_create_customer_rejects_unparseable_phone():
    """
    An input that doesn't plausibly represent a real SA mobile number must
    be rejected (422), not silently used as-is as a loyalty identity key —
    matches the existing digital-receipt convention (routes_pos.py).
    """
    business_id = uuid.uuid4()
    db = FakeCustomerDB(existing=None)
    with pytest.raises(AppError) as exc:
        await get_or_create_customer_by_phone(db, business_id, "not-a-phone-number")
    assert exc.value.code == "INVALID_PHONE_NUMBER"
    assert db.added == []
