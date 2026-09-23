"""
Loyalty routes (backend/app/api/v1/routes_loyalty.py) — program config,
reward catalogue CRUD, and the staff-facing customer-lookup/redeem
endpoints the POS will call.
"""
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_loyalty import (
    LoyaltyProgramUpdate,
    LoyaltyRewardCreate,
    RedeemRequest,
    create_reward,
    delete_reward,
    get_loyalty_program,
    lookup_customer_by_phone,
    redeem_loyalty_reward,
    update_loyalty_program,
)
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        if isinstance(obj, LoyaltyProgram):
            defaults = dict(
                is_enabled=False, points_per_rand=1, points_per_order=0,
                points_expiry_days=None, min_redemption_points=0,
                silver_tier_points=500, gold_tier_points=2000,
                birthday_reward_points=None, referral_reward_points=None,
                double_points_promo_active=False,
                double_points_promo_starts_at=None, double_points_promo_ends_at=None,
            )
            for field, value in defaults.items():
                if getattr(obj, field, None) is None:
                    setattr(obj, field, value)
        if isinstance(obj, LoyaltyReward) and getattr(obj, "is_active", None) is None:
            obj.is_active = True
        if isinstance(obj, Customer):
            if getattr(obj, "loyalty_points_balance", None) is None:
                obj.loyalty_points_balance = 0
            if getattr(obj, "loyalty_tier", None) is None:
                obj.loyalty_tier = "BRONZE"

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)

    async def flush(self):
        pass


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_program(business_id, **overrides):
    p = LoyaltyProgram(business_id=business_id)
    FakeDB([])._finalize(p)
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


# ── Program config ───────────────────────────────────────────────────────────

async def test_get_program_creates_default_disabled_program_when_none_exists():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])  # no existing program

    result = await get_loyalty_program(user=user, db=db)

    assert result.is_enabled is False
    assert db.commits == 1


async def test_get_program_returns_existing():
    user = make_user()
    program = make_program(user.business_id, is_enabled=True, points_per_rand=3)
    db = FakeDB([FakeResult(scalar=program)])

    result = await get_loyalty_program(user=user, db=db)

    assert result.is_enabled is True
    assert result.points_per_rand == 3
    assert db.commits == 0  # found existing — no create-commit needed


async def test_update_program_applies_only_provided_fields():
    user = make_user()
    program = make_program(user.business_id, is_enabled=False, points_per_rand=1)
    db = FakeDB([FakeResult(scalar=program)])

    result = await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=5), user=user, db=db,
    )

    assert result.is_enabled is True
    assert result.points_per_rand == 5


# ── Reward catalogue ─────────────────────────────────────────────────────────

async def test_create_reward():
    user = make_user()
    db = FakeDB([])

    result = await create_reward(
        body=LoyaltyRewardCreate(name="Free Coffee", points_cost=100), user=user, db=db,
    )

    assert result.name == "Free Coffee"
    assert result.points_cost == 100
    assert result.is_active is True


async def test_delete_reward_soft_deletes():
    user = make_user()
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=100)
    FakeDB([])._finalize(reward)
    db = FakeDB([FakeResult(scalar=reward)])

    await delete_reward(reward_id=reward.id, user=user, db=db)

    assert reward.is_active is False


async def test_delete_unknown_reward_is_404():
    user = make_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await delete_reward(reward_id=uuid.uuid4(), user=user, db=db)


# ── Staff-facing customer lookup + redeem ───────────────────────────────────

async def test_lookup_customer_is_404_when_loyalty_disabled():
    user = make_user(role="STAFF")
    program = make_program(user.business_id, is_enabled=False)
    db = FakeDB([FakeResult(scalar=program)])

    with pytest.raises(NotFoundError):
        await lookup_customer_by_phone(phone="0821234567", user=user, db=db)


async def test_lookup_customer_creates_when_enabled_and_none_found():
    user = make_user(role="STAFF")
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([FakeResult(scalar=program), FakeResult(scalar=None)])

    result = await lookup_customer_by_phone(phone="0821234567", user=user, db=db)

    # BUG-0098 fix: get_or_create_customer_by_phone now normalises through
    # normalize_sa_mobile before lookup/creation, so the stored (and
    # returned) phone_number is the canonical +27... form, not the raw
    # local-format string the till operator typed.
    assert result.phone_number == "+27821234567"
    assert result.loyalty_points_balance == 0


async def test_lookup_customer_rejects_unparseable_phone():
    user = make_user(role="STAFF")
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([FakeResult(scalar=program)])

    from backend.app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        await lookup_customer_by_phone(phone="not-a-phone", user=user, db=db)
    assert exc.value.code == "INVALID_PHONE_NUMBER"


async def test_redeem_via_route_success():
    user = make_user(role="STAFF")
    program = make_program(user.business_id, is_enabled=True)
    customer = Customer(business_id=user.business_id, phone_number="0821234567", loyalty_points_balance=500)
    FakeDB([])._finalize(customer)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=200)
    FakeDB([])._finalize(reward)

    # Query order: program lookup, unlocked customer lookup (404 check),
    # reward lookup, then redeem_reward()'s OWN row-lock reselect of the
    # SAME customer (BUG-0097 fix) — four executes, not three.
    db = FakeDB([
        FakeResult(scalar=program), FakeResult(scalar=customer),
        FakeResult(scalar=reward), FakeResult(scalar=customer),
    ])

    result = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id), user=user, db=db,
    )

    assert result.success is True
    assert result.balance_after == 300
    assert customer.loyalty_points_balance == 300
