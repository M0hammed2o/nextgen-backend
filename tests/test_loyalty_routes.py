"""
Loyalty routes (backend/app/api/v1/routes_loyalty.py) — program config,
reward catalogue CRUD, and the staff-facing customer-lookup/redeem
endpoints the POS will call.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1 import routes_loyalty as routes
from backend.app.api.v1.routes_loyalty import (
    LoyaltyAdjustmentRequest,
    LoyaltyProgramUpdate,
    LoyaltyRewardCreate,
    LoyaltyRewardUpdate,
    RedeemRequest,
    adjust_customer_points,
    create_reward,
    delete_reward,
    get_loyalty_program,
    list_rewards,
    lookup_customer_by_phone,
    redeem_loyalty_reward,
    update_loyalty_program,
    update_reward,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    def __init__(self, results, commit_exc=None):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._commit_exc = commit_exc
        self._commit_calls = 0

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
        self._commit_calls += 1
        if self._commit_exc is not None and self._commit_calls == 1:
            raise self._commit_exc
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

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


def make_business(business_id=None, **overrides):
    """
    Stands in for the Business row `_require_loyalty_enabled()` (migration
    0051, KNOWN_BUGS.md BUG-0107) now fetches FIRST in every route in this
    file. Defaults loyalty_enabled=True so every pre-existing test in this
    file (written before the entitlement gate existed) keeps exercising its
    own actual behaviour rather than being short-circuited by the gate —
    the gate's OWN refusal behaviour is covered by the dedicated
    `TestLoyaltyEntitlementGate` tests below.
    """
    defaults = dict(id=business_id or uuid.uuid4(), loyalty_enabled=True)
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


# ── Program config ───────────────────────────────────────────────────────────

async def test_get_program_creates_default_disabled_program_when_none_exists():
    user = make_user()
    business = make_business(user.business_id)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])  # no existing program

    result = await get_loyalty_program(user=user, db=db)

    assert result.is_enabled is False
    assert db.commits == 1


async def test_get_program_returns_existing():
    user = make_user()
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True, points_per_rand=3)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=program)])

    result = await get_loyalty_program(user=user, db=db)

    assert result.is_enabled is True
    assert result.points_per_rand == 3
    assert db.commits == 0  # found existing — no create-commit needed


async def test_update_program_applies_only_provided_fields():
    user = make_user()
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=False, points_per_rand=1)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=program)])

    result = await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=5), user=user, db=db,
    )

    assert result.is_enabled is True
    assert result.points_per_rand == 5


# ── Reward catalogue ─────────────────────────────────────────────────────────

async def test_create_reward():
    user = make_user()
    business = make_business(user.business_id)
    db = FakeDB([FakeResult(scalar=business)])

    result = await create_reward(
        body=LoyaltyRewardCreate(name="Free Coffee", points_cost=100), user=user, db=db,
    )

    assert result.name == "Free Coffee"
    assert result.points_cost == 100
    assert result.is_active is True


async def test_delete_reward_soft_deletes():
    user = make_user()
    business = make_business(user.business_id)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=100)
    FakeDB([])._finalize(reward)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=reward)])

    await delete_reward(reward_id=reward.id, user=user, db=db)

    assert reward.is_active is False


async def test_delete_unknown_reward_is_404():
    user = make_user()
    business = make_business(user.business_id)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await delete_reward(reward_id=uuid.uuid4(), user=user, db=db)


# ── Staff-facing customer lookup + redeem ───────────────────────────────────

async def test_lookup_customer_is_404_when_loyalty_disabled():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=False)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=program)])

    with pytest.raises(NotFoundError):
        await lookup_customer_by_phone(phone="0821234567", user=user, db=db)


async def test_lookup_customer_creates_when_enabled_and_none_found():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=program), FakeResult(scalar=None)])

    result = await lookup_customer_by_phone(phone="0821234567", user=user, db=db)

    # BUG-0100 fix: get_or_create_customer_by_phone now normalises through
    # normalize_sa_mobile before lookup/creation, so the stored (and
    # returned) phone_number is the canonical +27... form, not the raw
    # local-format string the till operator typed.
    assert result.phone_number == "+27821234567"
    assert result.loyalty_points_balance == 0


async def test_lookup_customer_rejects_unparseable_phone():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=program)])

    from backend.app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        await lookup_customer_by_phone(phone="not-a-phone", user=user, db=db)
    assert exc.value.code == "INVALID_PHONE_NUMBER"


async def test_redeem_via_route_success():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = Customer(business_id=user.business_id, phone_number="0821234567", loyalty_points_balance=500)
    FakeDB([])._finalize(customer)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=200)
    FakeDB([])._finalize(reward)

    # Query order: business entitlement lookup, program lookup, unlocked
    # customer lookup (404 check), reward lookup, BUG-0186 idempotency
    # pre-check (none found), then redeem_reward()'s OWN row-lock reselect of
    # the SAME customer (BUG-0097 fix) — six executes.
    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=program), FakeResult(scalar=customer),
        FakeResult(scalar=reward), FakeResult(scalar=None),
        FakeResult(scalar=customer),
    ])

    result = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id, idempotency_key="till-redeem-1"),
        user=user, db=db,
    )

    assert result.success is True
    assert result.balance_after == 300
    assert customer.loyalty_points_balance == 300


# ── BUG-0186: manual redeem idempotency, insufficient balance, tenant isolation ─

async def test_redeem_idempotent_replay_on_repeated_key():
    """A retried/double-clicked request carrying the SAME idempotency_key as
    an already-applied redemption must replay the original outcome, not
    deduct points a second time — the common-case pre-check path."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = make_customer(user.business_id, loyalty_points_balance=159)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Fries", points_cost=150)
    FakeDB([])._finalize(reward)
    existing_txn = LoyaltyTransaction(
        business_id=user.business_id, customer_id=customer.id, order_id=None,
        transaction_type="REDEEM", points_delta=-150, balance_after=9,
        reason="Redeemed: Free Fries", idempotency_key="REDEEM_MANUAL:fries-1",
    )
    FakeDB([])._finalize(existing_txn)

    db = FakeDB([
        FakeResult(scalar=business), FakeResult(scalar=program),
        FakeResult(scalar=customer), FakeResult(scalar=reward),
        FakeResult(scalar=existing_txn),  # idempotency pre-check finds the original
    ])

    result = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id, idempotency_key="fries-1"),
        user=user, db=db,
    )

    assert result.success is True
    assert result.points_delta == -150
    assert result.balance_after == 9
    # No new work done — this is a replay, not a second redemption.
    assert customer.loyalty_points_balance == 159
    assert db.added == []
    assert db.commits == 0


async def test_redeem_intentional_second_redemption_uses_new_key():
    """A DIFFERENT idempotency_key for a genuinely new redemption must NOT
    be short-circuited by an unrelated prior transaction — only an exact key
    match replays."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = make_customer(user.business_id, loyalty_points_balance=400)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Fries", points_cost=150)
    FakeDB([])._finalize(reward)

    db = FakeDB([
        FakeResult(scalar=business), FakeResult(scalar=program),
        FakeResult(scalar=customer), FakeResult(scalar=reward),
        FakeResult(scalar=None),  # no prior transaction under this NEW key
        FakeResult(scalar=customer),  # redeem_reward()'s row-lock reselect
    ])

    result = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id, idempotency_key="fries-2-genuinely-new"),
        user=user, db=db,
    )

    assert result.success is True
    assert result.balance_after == 250
    assert customer.loyalty_points_balance == 250
    assert db.commits == 1


async def test_redeem_race_caught_by_unique_index_returns_committed_state():
    """Two concurrent requests carrying the SAME idempotency_key both pass
    the pre-check before either has committed — the partial unique index
    decides the winner; the loser's commit hits it and must replay the
    winner's committed result instead of 500ing or double-deducting."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = make_customer(user.business_id, loyalty_points_balance=309)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Fries", points_cost=150)
    FakeDB([])._finalize(reward)
    winner_txn = LoyaltyTransaction(
        business_id=user.business_id, customer_id=customer.id, order_id=None,
        transaction_type="REDEEM", points_delta=-150, balance_after=159,
        reason="Redeemed: Free Fries", idempotency_key="REDEEM_MANUAL:race-key",
    )
    FakeDB([])._finalize(winner_txn)

    db = FakeDB(
        [
            FakeResult(scalar=business), FakeResult(scalar=program),
            FakeResult(scalar=customer), FakeResult(scalar=reward),
            FakeResult(scalar=None),          # pre-check passes (race window)
            FakeResult(scalar=customer),       # redeem_reward()'s row-lock reselect
            FakeResult(scalar=winner_txn),     # post-IntegrityError lookup finds the winner
        ],
        commit_exc=make_integrity_error(),
    )

    result = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id, idempotency_key="race-key"),
        user=user, db=db,
    )

    assert result.success is True
    assert result.points_delta == -150
    assert result.balance_after == 159
    assert db.rollbacks == 1
    assert db.commits == 0


async def test_redeem_insufficient_points_rejected_and_untouched():
    """Route-layer coverage for the insufficient-balance path (engine-level
    behaviour already covered in test_loyalty_engine.py) — the balance must
    be left completely untouched and nothing written."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = make_customer(user.business_id, loyalty_points_balance=50)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Fries", points_cost=150)
    FakeDB([])._finalize(reward)

    db = FakeDB([
        FakeResult(scalar=business), FakeResult(scalar=program),
        FakeResult(scalar=customer), FakeResult(scalar=reward),
        FakeResult(scalar=None),      # idempotency pre-check — no prior attempt
        FakeResult(scalar=customer),  # redeem_reward()'s row-lock reselect
    ])

    with pytest.raises(AppError) as exc:
        await redeem_loyalty_reward(
            body=RedeemRequest(customer_id=customer.id, reward_id=reward.id, idempotency_key="not-enough-points"),
            user=user, db=db,
        )
    assert exc.value.code == "INSUFFICIENT_POINTS"
    assert customer.loyalty_points_balance == 50  # untouched
    assert db.added == []
    assert db.commits == 0


async def test_redeem_customer_in_another_business_is_not_found():
    """A customer_id belonging to a different business must be
    indistinguishable from one that does not exist — the lookup is scoped by
    user.business_id (the JWT claim), never by client input, so a
    cross-tenant id resolves to the exact same NotFoundError a nonexistent
    id would."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    other_business_customer_id = uuid.uuid4()

    db = FakeDB([
        FakeResult(scalar=business), FakeResult(scalar=program),
        FakeResult(scalar=None),  # business_id filter excludes the other tenant's row
    ])

    with pytest.raises(NotFoundError) as exc:
        await redeem_loyalty_reward(
            body=RedeemRequest(
                customer_id=other_business_customer_id, reward_id=uuid.uuid4(), idempotency_key="cross-tenant-probe",
            ),
            user=user, db=db,
        )
    assert exc.value.code == "CUSTOMER_NOT_FOUND"
    assert db.added == []
    assert db.commits == 0


async def test_redeem_reward_in_another_business_is_not_found():
    """Same tenant-isolation guarantee for the reward side of the lookup —
    a reward_id belonging to a different business must 404, not redeem
    someone else's reward catalogue entry against this business's customer."""
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    customer = make_customer(user.business_id, loyalty_points_balance=500)
    other_business_reward_id = uuid.uuid4()

    db = FakeDB([
        FakeResult(scalar=business), FakeResult(scalar=program),
        FakeResult(scalar=customer),
        FakeResult(scalar=None),  # business_id filter excludes the other tenant's reward
    ])

    with pytest.raises(NotFoundError) as exc:
        await redeem_loyalty_reward(
            body=RedeemRequest(
                customer_id=customer.id, reward_id=other_business_reward_id, idempotency_key="cross-tenant-reward",
            ),
            user=user, db=db,
        )
    assert exc.value.code == "LOYALTYREWARD_NOT_FOUND"
    assert customer.loyalty_points_balance == 500  # untouched
    assert db.added == []
    assert db.commits == 0


# ── Manual points adjustment (adjust_customer_points) ───────────────────────
# Closes DECISIONS.md DECISION-0034's "known gap" — see engine-level tests
# in test_loyalty_engine.py for the business-logic behaviour; these exercise
# the route layer: tenant isolation, role gating, and idempotency replay.

def make_customer(business_id, **overrides):
    c = Customer(business_id=business_id, phone_number="0821234567", loyalty_points_balance=500)
    FakeDB([])._finalize(c)
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def make_integrity_error(constraint_name="uq_loyalty_transactions_business_id_idempotency_key") -> IntegrityError:
    return IntegrityError(
        "INSERT INTO loyalty_transactions ...", {},
        Exception(f'duplicate key value violates unique constraint "{constraint_name}"'),
    )


def test_adjust_points_route_is_owner_or_manager_only():
    """Introspects the actual Depends() closure, not just the decorator —
    same technique as tests/test_routes_delivery.py::TestRoleGating, since
    calling a route function directly (as every other test in this file
    does) bypasses FastAPI's Depends() resolution entirely."""
    for route in routes.router.routes:
        if route.path == "/business/loyalty/customers/{customer_id}/adjust-points" and "POST" in route.methods:
            for d in route.dependant.dependencies:
                if d.call.__name__ == "_check":
                    freevars = d.call.__code__.co_freevars
                    closure = d.call.__closure__
                    mapping = dict(zip(freevars, [c.cell_contents for c in closure])) if closure else {}
                    assert mapping.get("allowed_roles") == ["OWNER", "MANAGER"]
                    return
    raise AssertionError("adjust-points route not found")


async def test_adjust_points_customer_in_another_business_is_not_found():
    """A customer_id that belongs to a different business must be
    indistinguishable from one that does not exist — business_id in the
    lookup comes from user.business_id (the JWT), never from the request,
    so a cross-tenant id and a nonexistent one both resolve to the exact
    same scalar_one_or_none()=None the query would genuinely return."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    other_business_customer_id = uuid.uuid4()
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])  # business_id filter excludes the other tenant's row

    with pytest.raises(NotFoundError) as exc:
        await adjust_customer_points(
            customer_id=other_business_customer_id,
            body=LoyaltyAdjustmentRequest(points_delta=50, reason="Should never be reached"),
            user=user, db=db,
        )
    assert exc.value.code == "CUSTOMER_NOT_FOUND"
    assert db.added == []
    assert db.commits == 0


async def test_adjust_points_route_rejects_zero_delta():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=customer), FakeResult(scalar=program)])

    with pytest.raises(AppError) as exc:
        await adjust_customer_points(
            customer_id=customer.id,
            body=LoyaltyAdjustmentRequest(points_delta=0, reason="No actual change"),
            user=user, db=db,
        )
    assert exc.value.code == "ZERO_ADJUSTMENT"
    assert db.added == []
    assert db.commits == 0


async def test_adjust_points_route_refuses_negative_balance_cleanly():
    user = make_user(role="MANAGER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id, loyalty_points_balance=100)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([
        FakeResult(scalar=business),   # entitlement gate
        FakeResult(scalar=customer),  # tenant-scoped lookup
        FakeResult(scalar=program),   # program lookup for tier recompute
        FakeResult(scalar=customer),  # adjust_points' own row-lock reselect
    ])

    with pytest.raises(AppError) as exc:
        await adjust_customer_points(
            customer_id=customer.id,
            body=LoyaltyAdjustmentRequest(points_delta=-500, reason="Overcorrection attempt"),
            user=user, db=db,
        )
    assert exc.value.code == "INSUFFICIENT_POINTS_FOR_ADJUSTMENT"
    assert customer.loyalty_points_balance == 100  # untouched
    assert db.added == []
    assert db.commits == 0


async def test_adjust_points_route_success_upward_writes_ledger_and_audit_event():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id, loyalty_points_balance=500)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=customer),
        FakeResult(scalar=program),
        FakeResult(scalar=customer),  # row-lock reselect
    ])

    result = await adjust_customer_points(
        customer_id=customer.id,
        body=LoyaltyAdjustmentRequest(points_delta=75, reason="Partial refund on order BO-000042, points not auto-reversed"),
        user=user, db=db,
    )

    assert result.customer_id == customer.id
    assert result.points_delta == 75
    assert result.balance_after == 575
    assert customer.loyalty_points_balance == 575
    assert db.commits == 1

    txns = [o for o in db.added if isinstance(o, LoyaltyTransaction)]
    assert len(txns) == 1
    assert txns[0].transaction_type == "ADJUST"
    assert txns[0].points_delta == 75
    assert txns[0].order_id is None

    from shared.models.audit import AuditEvent
    audits = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audits) == 1
    assert audits[0].action == "loyalty.points.adjusted"
    assert audits[0].business_id == user.business_id
    assert audits[0].actor_user_id == user.user_id
    assert audits[0].target_id == customer.id
    assert audits[0].diff_json["points_delta"] == 75
    assert audits[0].diff_json["balance_after"] == 575


async def test_adjust_points_route_success_downward():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id, loyalty_points_balance=500)
    program = make_program(user.business_id, is_enabled=True)
    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=customer),
        FakeResult(scalar=program),
        FakeResult(scalar=customer),
    ])

    result = await adjust_customer_points(
        customer_id=customer.id,
        body=LoyaltyAdjustmentRequest(points_delta=-120, reason="Correcting a duplicate manual EARN"),
        user=user, db=db,
    )

    assert result.points_delta == -120
    assert result.balance_after == 380
    assert customer.loyalty_points_balance == 380


async def test_adjust_points_route_idempotent_replay_on_repeated_key():
    """A request carrying the SAME idempotency_key as an already-applied
    adjustment must replay the original result, not apply a second one —
    the common-case pre-check path (no race, no IntegrityError)."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id, loyalty_points_balance=500)
    existing_txn = LoyaltyTransaction(
        business_id=user.business_id, customer_id=customer.id, order_id=None,
        transaction_type="ADJUST", points_delta=30, balance_after=530,
        reason="Already applied", idempotency_key="ADJUST:form-key-1",
    )
    FakeDB([])._finalize(existing_txn)

    db = FakeDB([
        FakeResult(scalar=business),       # entitlement gate
        FakeResult(scalar=customer),       # tenant-scoped lookup
        FakeResult(scalar=existing_txn),   # idempotency pre-check finds the original
    ])

    result = await adjust_customer_points(
        customer_id=customer.id,
        body=LoyaltyAdjustmentRequest(points_delta=30, reason="Retried form submission", idempotency_key="form-key-1"),
        user=user, db=db,
    )

    assert result.points_delta == 30
    assert result.balance_after == 530
    # No new work done — the customer's balance was never touched by this
    # (replayed) call, and nothing new was added/committed.
    assert customer.loyalty_points_balance == 500
    assert db.added == []
    assert db.commits == 0


async def test_adjust_points_route_race_caught_by_unique_index_returns_committed_state():
    """Two concurrent requests carrying the SAME idempotency_key both pass
    the pre-check before either has committed — the partial unique index
    is what actually decides the winner; the loser's commit hits it and
    must replay the winner's committed result instead of 500ing or
    double-adjusting."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    customer = make_customer(user.business_id, loyalty_points_balance=500)
    winner_txn = LoyaltyTransaction(
        business_id=user.business_id, customer_id=customer.id, order_id=None,
        transaction_type="ADJUST", points_delta=40, balance_after=540,
        reason="Winner of the race", idempotency_key="ADJUST:race-key",
    )
    FakeDB([])._finalize(winner_txn)

    db = FakeDB(
        [
            FakeResult(scalar=business),     # entitlement gate
            FakeResult(scalar=customer),     # tenant-scoped lookup
            FakeResult(scalar=None),         # pre-check passes (race window)
            FakeResult(scalar=None),         # program lookup — none configured
            FakeResult(scalar=customer),     # adjust_points' own row-lock reselect
            FakeResult(scalar=winner_txn),   # post-IntegrityError lookup finds the winner
        ],
        commit_exc=make_integrity_error(),
    )

    result = await adjust_customer_points(
        customer_id=customer.id,
        body=LoyaltyAdjustmentRequest(points_delta=40, reason="Loser of the race", idempotency_key="race-key"),
        user=user, db=db,
    )

    assert result.points_delta == 40
    assert result.balance_after == 540
    assert db.rollbacks == 1
    assert db.commits == 0


# ── Missing route-layer coverage: list_rewards / update_reward ─────────────
# Neither had a dedicated test in this file before the entitlement gate was
# added — closing that gap in the same pass, per this task's "add coverage
# that would catch a sixth [missing-schema-field] occurrence" instruction.

class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _ListResult:
    """FakeResult's scalar_one_or_none() shape doesn't cover list_rewards'
    select(...).scalars().all() call — this stands in for that specific
    query shape only."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


async def test_list_rewards_returns_only_active_rewards_for_this_business():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=100)
    FakeDB([])._finalize(reward)
    db = FakeDB([FakeResult(scalar=business)])
    db._results.append(_ListResult([reward]))

    result = await list_rewards(user=user, db=db)

    assert len(result) == 1
    assert result[0].name == "Free Coffee"


async def test_update_reward_applies_only_provided_fields():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    reward = LoyaltyReward(business_id=user.business_id, name="Free Coffee", points_cost=100)
    FakeDB([])._finalize(reward)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=reward)])

    result = await update_reward(
        reward_id=reward.id, body=LoyaltyRewardUpdate(points_cost=150), user=user, db=db,
    )

    assert result.points_cost == 150
    assert result.name == "Free Coffee"  # untouched


async def test_update_reward_unknown_is_404():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

    with pytest.raises(NotFoundError):
        await update_reward(reward_id=uuid.uuid4(), body=LoyaltyRewardUpdate(points_cost=1), user=user, db=db)


# ── Entitlement gate refusal (migration 0051, KNOWN_BUGS.md BUG-0107) ──────
# Every route in this file must refuse with 409 LOYALTY_NOT_ENABLED — checked
# FIRST, before any other query or mutation — for a business the platform
# has not entitled to the Loyalty module. Each test below gives FakeDB only
# the single business-lookup result, proving the route reaches nowhere else
# (a second execute() call would IndexError on an empty queue).

class TestLoyaltyEntitlementGate:
    async def test_get_program_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await get_loyalty_program(user=user, db=db)
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert exc.value.status_code == 409

    async def test_update_program_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await update_loyalty_program(
                body=LoyaltyProgramUpdate(is_enabled=True), user=user, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert db.commits == 0
        assert db.added == []

    async def test_list_rewards_refused_for_non_entitled_business(self):
        user = make_user(role="STAFF")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await list_rewards(user=user, db=db)
        assert exc.value.code == "LOYALTY_NOT_ENABLED"

    async def test_create_reward_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await create_reward(
                body=LoyaltyRewardCreate(name="Free Coffee", points_cost=100), user=user, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert db.commits == 0
        assert db.added == []

    async def test_update_reward_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await update_reward(
                reward_id=uuid.uuid4(), body=LoyaltyRewardUpdate(points_cost=1), user=user, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"

    async def test_delete_reward_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await delete_reward(reward_id=uuid.uuid4(), user=user, db=db)
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert db.commits == 0

    async def test_lookup_customer_refused_for_non_entitled_business(self):
        user = make_user(role="STAFF")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await lookup_customer_by_phone(phone="0821234567", user=user, db=db)
        assert exc.value.code == "LOYALTY_NOT_ENABLED"

    async def test_redeem_refused_for_non_entitled_business(self):
        user = make_user(role="STAFF")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await redeem_loyalty_reward(
                body=RedeemRequest(
                    customer_id=uuid.uuid4(), reward_id=uuid.uuid4(), idempotency_key="gate-check",
                ),
                user=user, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert db.commits == 0

    async def test_adjust_points_refused_for_non_entitled_business(self):
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=False)
        db = FakeDB([FakeResult(scalar=business)])

        with pytest.raises(AppError) as exc:
            await adjust_customer_points(
                customer_id=uuid.uuid4(),
                body=LoyaltyAdjustmentRequest(points_delta=10, reason="Should never be reached"),
                user=user, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert db.commits == 0
        assert db.added == []

    async def test_get_program_business_not_found_is_404(self):
        """A business_id that resolves to no row at all (should never happen
        for a real JWT, but defensive) is a distinct 404, not a 409."""
        user = make_user(role="OWNER")
        db = FakeDB([FakeResult(scalar=None)])

        with pytest.raises(NotFoundError):
            await get_loyalty_program(user=user, db=db)

    async def test_get_program_succeeds_for_entitled_business(self):
        """Positive control — an entitled business is unaffected by the
        gate and reaches its normal program-fetch/create behaviour."""
        user = make_user(role="OWNER")
        business = make_business(user.business_id, loyalty_enabled=True)
        db = FakeDB([FakeResult(scalar=business), FakeResult(scalar=None)])

        result = await get_loyalty_program(user=user, db=db)

        assert result.is_enabled is False
        assert db.commits == 1
