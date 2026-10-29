"""
Loyalty Programme routes — program configuration, reward catalogue, and
staff-facing customer lookup/redemption for the POS.

Program config and reward catalogue are OWNER/MANAGER-only (business
decisions about earning/redemption rules); looking up a customer's balance
and redeeming a reward are STAFF-or-above (till-floor actions).

`extra="forbid"` decision (2026-09-23, made while fixing the `value_cents`
exposure gap below): deliberately NOT added to any schema in this file.
A future frontend field silently landing on the wrong schema (exactly this
bug) would have been caught immediately as a 422 rather than a silent drop
— a real argument for it, and this is now the documented fourth occurrence
of a `Business`/model column existing with no schema exposing it on this
platform. Not applied here because the blast radius of rejecting any
currently-tolerated extra field from an existing frontend request is
unverified from this backend-only session (would need to read the actual
request shapes `nextgen-manager-v3` sends, out of this task's scope) —
flipping write validation from "ignore extras" to "reject extras" is a
behavioural change with real risk of breaking an unrelated in-flight
request shape, not something to do incidentally while fixing an unrelated
missing-field bug. Recorded as a considered, deliberate follow-up in
TODO.md rather than applied silently.

`Business.loyalty_enabled` entitlement gate (2026-09-23, migration 0051,
closing KNOWN_BUGS.md BUG-0107): every route below now calls
`_require_loyalty_enabled()` FIRST — programme read/update, reward
list/create/update/delete, customer lookup, `/redeem`, and manual points
adjustment all refuse with 409 `LOYALTY_NOT_ENABLED` for a business the
platform hasn't entitled. The POS checkout redemption path
(`routes_pos.py::create_pos_order`, when `loyalty_reward_id` is supplied)
carries the identical gate, checked before any customer/points mutation.

DELIBERATE CARVE-OUT — points EARNING is NOT gated by `loyalty_enabled`:
`backend/app/loyalty/engine.py::award_points_for_paid_order` /
`award_loyalty_points_if_enabled` remain governed ONLY by
`LoyaltyProgram.is_enabled`, exactly as before this change. Reasoning:
  1. Going forward this is close to moot — `PUT /business/loyalty/program`
     (the only way to flip `LoyaltyProgram.is_enabled` to True) is now
     itself gated on `loyalty_enabled`, so a business can no longer reach
     `is_enabled=True` in the first place without the platform entitlement.
  2. The one real remaining case is a business that WAS entitled, enabled
     the programme, and is later downgraded (`loyalty_enabled` flipped back
     to False by a platform admin) without anyone also disabling
     `LoyaltyProgram.is_enabled`. Silently gating earning here would mean a
     customer's points quietly stop accruing on every paid order with no
     visible signal to the till operator or the customer — a materially
     worse failure mode than an explicit, visible 409 on a redemption or
     configuration request, which is exactly what a REFUSED action should
     look like. Earning is a passive, automatic side effect of a payment
     confirmation that must never fail a payment
     (`award_loyalty_points_if_enabled`'s own docstring: "this must never be
     able to block a payment confirmation").
  3. A silent earning stop creates a PERMANENT, non-backfillable gap in a
     customer's balance: unlike redemption (which just resumes correctly
     the moment entitlement returns) there is no honest way to later
     reconstruct "how many points would this customer have earned during
     the gap." A tenant who is re-entitled (upgrades again) would otherwise
     face inexplicable customer complaints about missing points with no
     correct fix available — worse than the commercial harm of a few extra
     orders continuing to accrue points into an already-existing wallet
     during a brief downgrade window, which costs the platform nothing
     (points are not cash) and is trivially correctable via the manual
     points-adjustment endpoint if it ever matters.
See DECISIONS.md for the same reasoning recorded as a standing decision.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.loyalty.engine import (
    adjust_points,
    get_or_create_customer_by_phone,
    manual_adjustment_idempotency_key,
    manual_redeem_idempotency_key,
    redeem_reward,
)
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction

router = APIRouter(prefix="/business/loyalty", tags=["loyalty"])


# ── Entitlement gate (migration 0051, KNOWN_BUGS.md BUG-0107) ────────────────

async def _require_loyalty_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    """
    Server-side commercial-package gate for the whole Loyalty module —
    mirrors `_require_inventory_enabled` (routes_recipes.py) /
    `require_advanced_analytics_enabled` (backend/app/analytics/revenue.py).
    Deliberately called FIRST in every route below, before any other query
    or mutation (including `_get_or_create_program`, which itself writes a
    row on first touch) — a non-entitled business must be refused before
    anything else happens, not merely before its money/points are touched.

    Does NOT gate points EARNING (`award_points_for_paid_order` /
    `award_loyalty_points_if_enabled` in backend/app/loyalty/engine.py) —
    that is a deliberate, separate decision, see this file's own module
    docstring addendum below and DECISIONS.md.
    """
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.loyalty_enabled:
        raise AppError(
            "LOYALTY_NOT_ENABLED",
            "The loyalty module is not enabled for this business",
            409,
        )
    return business


# ── Schemas ──────────────────────────────────────────────────────────────────

class LoyaltyProgramResponse(BaseModel):
    is_enabled: bool
    points_per_rand: int
    points_per_order: int
    points_expiry_days: int | None
    min_redemption_points: int
    silver_tier_points: int
    gold_tier_points: int
    birthday_reward_points: int | None
    referral_reward_points: int | None
    double_points_promo_active: bool
    double_points_promo_starts_at: str | None = None
    double_points_promo_ends_at: str | None = None
    model_config = {"from_attributes": True}


class LoyaltyProgramUpdate(BaseModel):
    is_enabled: bool | None = None
    points_per_rand: int | None = Field(default=None, ge=0)
    points_per_order: int | None = Field(default=None, ge=0)
    points_expiry_days: int | None = Field(default=None, ge=1)
    min_redemption_points: int | None = Field(default=None, ge=0)
    silver_tier_points: int | None = Field(default=None, ge=0)
    gold_tier_points: int | None = Field(default=None, ge=0)
    birthday_reward_points: int | None = Field(default=None, ge=0)
    referral_reward_points: int | None = Field(default=None, ge=0)
    double_points_promo_active: bool | None = None
    double_points_promo_starts_at: str | None = None
    double_points_promo_ends_at: str | None = None


class LoyaltyRewardCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = None
    points_cost: int = Field(ge=1)
    # Added 2026-09-23 — `LoyaltyReward.value_cents` (nullable, migration
    # `0050_loyalty_pos_redemption`) was already read/enforced by
    # routes_pos.py's checkout redemption path, but this schema never
    # declared it, so it was silently dropped on every create/update
    # (Pydantic ignores unknown fields by default — no `extra="forbid"` was
    # set) and every reward was therefore permanently unredeemable at the
    # till. See KNOWN_BUGS.md for the bug record. NULL stays the default —
    # never default to 0, which would silently turn a manual-handover-only
    # reward into a real R0 till discount (matches the model column's own
    # docstring).
    value_cents: int | None = Field(
        default=None, ge=0,
        description="Cents discount applied at POS checkout when redeemed. "
                     "Omit or send null for a manual-handover-only reward "
                     "(not redeemable at the till).",
    )


class LoyaltyRewardUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    points_cost: int | None = Field(default=None, ge=1)
    is_active: bool | None = None
    # Same gap as LoyaltyRewardCreate.value_cents above — writable here too,
    # including explicitly clearing it back to NULL (manual-handover-only).
    # `exclude_unset=True` (used by both PUT handlers below) means this only
    # applies when the client actually sends the key, so `null` clears it
    # while an omitted key leaves the stored value untouched, exactly like
    # every other optional field on this schema.
    value_cents: int | None = Field(default=None, ge=0)


class LoyaltyRewardResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    points_cost: int
    is_active: bool
    value_cents: int | None
    model_config = {"from_attributes": True}


class CustomerLoyaltyResponse(BaseModel):
    customer_id: uuid.UUID
    display_name: str | None
    phone_number: str | None
    loyalty_points_balance: int
    loyalty_tier: str


class RedeemRequest(BaseModel):
    customer_id: uuid.UUID
    reward_id: uuid.UUID
    # Required (BUG-0186) — a retried/double-clicked request with the SAME key
    # replays the original outcome instead of deducting points twice; an
    # intentional second redemption must use a new key. Mirrors the POS
    # checkout path's own mandatory idempotency_key for a loyalty redemption
    # (routes_pos.py::POSOrderCreateRequest.validate_loyalty_redemption).
    idempotency_key: str = Field(min_length=1, max_length=160)


class RedeemResponse(BaseModel):
    success: bool
    points_delta: int
    balance_after: int


class LoyaltyAdjustmentRequest(BaseModel):
    """
    A manual owner/manager correction to a customer's points balance — up
    or down. Closes the gap left by DECISIONS.md DECISION-0034: a partial
    refund deliberately never touches points automatically (there is no
    honest pro-rata answer), so a manager who needs to correct the
    resulting balance had no API to do it with before this endpoint.
    """
    points_delta: int = Field(
        description="Signed points change. Positive credits points, negative debits them. Cannot be zero."
    )
    reason: str = Field(
        min_length=3, max_length=255,
        description="Mandatory — why this correction was made (e.g. 'Partial refund on order BO-000123, "
                     "reward points not auto-reversed')",
    )
    idempotency_key: str | None = Field(
        default=None, max_length=140,
        description="Optional. Supply the same key on a retried/double-submitted correction to make the "
                     "second attempt a no-op replay of the first rather than a second adjustment.",
    )


class LoyaltyAdjustmentResponse(BaseModel):
    customer_id: uuid.UUID
    points_delta: int
    balance_after: int
    reason: str | None


# ── Program config ───────────────────────────────────────────────────────────

async def _get_or_create_program(db: AsyncSession, business_id: uuid.UUID) -> LoyaltyProgram:
    result = await db.execute(select(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id))
    program = result.scalar_one_or_none()
    if program:
        return program
    program = LoyaltyProgram(business_id=business_id)
    db.add(program)
    await db.commit()
    await db.refresh(program)
    return program


@router.get("/program", response_model=LoyaltyProgramResponse)
async def get_loyalty_program(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    program = await _get_or_create_program(db, user.business_id)
    return LoyaltyProgramResponse.model_validate(program)


@router.put("/program", response_model=LoyaltyProgramResponse)
async def update_loyalty_program(
    body: LoyaltyProgramUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    program = await _get_or_create_program(db, user.business_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(program, field, value)
    await db.commit()
    await db.refresh(program)
    return LoyaltyProgramResponse.model_validate(program)


# ── Reward catalogue ─────────────────────────────────────────────────────────

@router.get("/rewards", response_model=list[LoyaltyRewardResponse])
async def list_rewards(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    result = await db.execute(
        select(LoyaltyReward)
        .where(LoyaltyReward.business_id == user.business_id, LoyaltyReward.is_active.is_(True))
        .order_by(LoyaltyReward.points_cost)
    )
    return [LoyaltyRewardResponse.model_validate(r) for r in result.scalars().all()]


@router.post("/rewards", response_model=LoyaltyRewardResponse, status_code=201)
async def create_reward(
    body: LoyaltyRewardCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    reward = LoyaltyReward(business_id=user.business_id, **body.model_dump())
    db.add(reward)
    await db.commit()
    await db.refresh(reward)
    return LoyaltyRewardResponse.model_validate(reward)


@router.put("/rewards/{reward_id}", response_model=LoyaltyRewardResponse)
async def update_reward(
    reward_id: uuid.UUID,
    body: LoyaltyRewardUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    result = await db.execute(
        select(LoyaltyReward).where(LoyaltyReward.id == reward_id, LoyaltyReward.business_id == user.business_id)
    )
    reward = result.scalar_one_or_none()
    if not reward:
        raise NotFoundError("LoyaltyReward", str(reward_id))
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(reward, field, value)
    await db.commit()
    await db.refresh(reward)
    return LoyaltyRewardResponse.model_validate(reward)


@router.delete("/rewards/{reward_id}", status_code=204)
async def delete_reward(
    reward_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_loyalty_enabled(db, user.business_id)
    result = await db.execute(
        select(LoyaltyReward).where(LoyaltyReward.id == reward_id, LoyaltyReward.business_id == user.business_id)
    )
    reward = result.scalar_one_or_none()
    if not reward:
        raise NotFoundError("LoyaltyReward", str(reward_id))
    reward.is_active = False
    await db.commit()


# ── Staff-facing: customer lookup + redemption (POS) ─────────────────────────

@router.get("/customers/lookup", response_model=CustomerLoyaltyResponse)
async def lookup_customer_by_phone(
    phone: str,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Find (or create, if loyalty is enabled) a customer by phone number, for
    a staff member to check a walk-in customer's points balance at the till.
    """
    await _require_loyalty_enabled(db, user.business_id)
    program = await _get_or_create_program(db, user.business_id)
    if not program.is_enabled:
        raise NotFoundError("LoyaltyProgram")

    customer = await get_or_create_customer_by_phone(db, user.business_id, phone)
    await db.commit()
    await db.refresh(customer)
    return CustomerLoyaltyResponse(
        customer_id=customer.id,
        display_name=customer.display_name,
        phone_number=customer.phone_number,
        loyalty_points_balance=customer.loyalty_points_balance,
        loyalty_tier=customer.loyalty_tier,
    )


@router.post("/redeem", response_model=RedeemResponse)
async def redeem_loyalty_reward(
    body: RedeemRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    BUG-0186 fix: a retried/double-clicked request with the same
    `idempotency_key` replays the original outcome instead of deducting
    points a second time. Checked up front (the common case, a sequential
    retry) and again by catching the unique-index violation on a genuine
    concurrent double-submit (the race case) — the exact two-layer pattern
    `adjust_customer_points` already uses below. The POS checkout redemption
    path (`routes_pos.py` -> `redeem_reward_for_checkout`) is untouched by
    this change: it always passes its own `order_id`-derived key, which
    `redeem_reward()` still prefers over any explicit key.
    """
    await _require_loyalty_enabled(db, user.business_id)
    program = await _get_or_create_program(db, user.business_id)

    cust_result = await db.execute(
        select(Customer).where(Customer.id == body.customer_id, Customer.business_id == user.business_id)
    )
    customer = cust_result.scalar_one_or_none()
    if not customer:
        raise NotFoundError("Customer", str(body.customer_id))

    reward_result = await db.execute(
        select(LoyaltyReward).where(LoyaltyReward.id == body.reward_id, LoyaltyReward.business_id == user.business_id)
    )
    reward = reward_result.scalar_one_or_none()
    if not reward:
        raise NotFoundError("LoyaltyReward", str(body.reward_id))

    namespaced_key = manual_redeem_idempotency_key(body.idempotency_key)

    existing = await _find_by_idempotency_key(db, user.business_id, namespaced_key)
    if existing is not None:
        return RedeemResponse(success=True, points_delta=existing.points_delta, balance_after=existing.balance_after)

    try:
        txn = await redeem_reward(db, user.business_id, customer, reward, program, idempotency_key=namespaced_key)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_adjustment_idempotency_violation(exc):
            existing = await _find_by_idempotency_key(db, user.business_id, namespaced_key)
            if existing is not None:
                return RedeemResponse(
                    success=True, points_delta=existing.points_delta, balance_after=existing.balance_after,
                )
        raise

    return RedeemResponse(success=True, points_delta=txn.points_delta, balance_after=txn.balance_after)


# ── Manual points adjustment (owner/manager) ─────────────────────────────────

def _is_adjustment_idempotency_violation(exc: IntegrityError) -> bool:
    """
    True if this IntegrityError is a violation of the loyalty ledger's
    partial unique index (business_id, idempotency_key) — checked against
    the driver's own error text, same driver-agnostic technique as
    backend/app/inventory/ledger.py::is_idempotency_violation and
    routes_pos.py::_is_unique_violation.
    """
    return "uq_loyalty_transactions_business_id_idempotency_key" in str(getattr(exc, "orig", exc))


async def _find_by_idempotency_key(
    db: AsyncSession, business_id: uuid.UUID, idempotency_key: str,
) -> LoyaltyTransaction | None:
    result = await db.execute(
        select(LoyaltyTransaction).where(
            LoyaltyTransaction.business_id == business_id,
            LoyaltyTransaction.idempotency_key == idempotency_key,
        )
    )
    return result.scalar_one_or_none()


@router.post(
    "/customers/{customer_id}/adjust-points",
    response_model=LoyaltyAdjustmentResponse,
    status_code=201,
)
async def adjust_customer_points(
    customer_id: uuid.UUID,
    body: LoyaltyAdjustmentRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Manually correct a customer's loyalty points balance, up or down, with
    a mandatory reason — see DECISIONS.md DECISION-0034 and TODO.md's
    Loyalty Programme section for why this exists: a partial refund
    deliberately never touches points automatically (no honest pro-rata
    answer exists), so before this endpoint a manager had no way at all to
    hand-correct the resulting balance short of direct database access.

    OWNER/MANAGER only (`require_owner_or_manager`) — this moves something
    of real value, unlike the till-floor lookup/redeem actions above, which
    are STAFF-or-above.

    `customer_id` is resolved against `Customer.business_id ==
    user.business_id` (the JWT claim, never client input) — a customer
    belonging to another business is indistinguishable from one that does
    not exist at all (404 `CUSTOMER_NOT_FOUND`), matching every other
    cross-tenant-probe-resistant lookup in this codebase.

    Gated on `Business.loyalty_enabled` (migration 0051) like every other
    route in this file — see `_require_loyalty_enabled` and this module's
    own docstring for why a manual correction is gated even though points
    EARNING deliberately is not.

    A zero `points_delta` is refused (`ZERO_ADJUSTMENT`, 422) rather than
    writing a meaningless ledger row, and a downward adjustment that would
    take the balance negative is refused cleanly (`INSUFFICIENT_POINTS_FOR_ADJUSTMENT`,
    422) rather than surfacing as a raw
    `ck_customers_loyalty_points_balance_nonneg` IntegrityError — both
    enforced in `backend/app/loyalty/engine.py::adjust_points`, which also
    takes the same customer row lock every other balance-mutating loyalty
    path uses, so this can never lost-update against a concurrent
    redemption/earn/other adjustment for the same customer.

    Idempotency: OPTIONAL `idempotency_key`. A human typing a one-off
    correction into a form is a different risk profile from an automated
    webhook retry — there is no automatic retry loop that could hammer this
    endpoint — but a double-submitted form (a manager double-clicking Save,
    or a flaky connection triggering a client-side retry) is a real,
    ordinary risk worth guarding against for a money-adjacent action. Reuses
    the exact `loyalty_transactions.idempotency_key` +
    `uq_loyalty_transactions_business_id_idempotency_key` partial unique
    index migration `0050` already added (namespaced via
    `manual_adjustment_idempotency_key` so it can never collide with a POS
    checkout's own `REDEEM:<order_id>`/`REDEEM_REVERSAL:<order_id>` keys in
    the same index) — a retried request with the SAME key replays the
    original result instead of applying a second adjustment, checked both
    up front (the common case) and by catching the unique-index violation
    on a genuine concurrent double-submit (the race case), following the
    exact two-layer pattern `routes_pos.py::create_pos_order`'s own
    idempotency replay uses.
    """
    await _require_loyalty_enabled(db, user.business_id)

    cust_result = await db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.business_id == user.business_id)
    )
    customer = cust_result.scalar_one_or_none()
    if not customer:
        raise NotFoundError("Customer", str(customer_id))

    namespaced_key = manual_adjustment_idempotency_key(body.idempotency_key) if body.idempotency_key else None

    if namespaced_key:
        existing = await _find_by_idempotency_key(db, user.business_id, namespaced_key)
        if existing is not None:
            return LoyaltyAdjustmentResponse(
                customer_id=existing.customer_id,
                points_delta=existing.points_delta,
                balance_after=existing.balance_after,
                reason=existing.reason,
            )

    program_result = await db.execute(select(LoyaltyProgram).where(LoyaltyProgram.business_id == user.business_id))
    program = program_result.scalar_one_or_none()

    try:
        txn = await adjust_points(
            db, user.business_id, customer, program,
            points_delta=body.points_delta, reason=body.reason, idempotency_key=namespaced_key,
        )
        db.add(
            AuditEvent(
                scope=AuditScope.BUSINESS.value,
                business_id=user.business_id,
                actor_user_id=user.user_id,
                action="loyalty.points.adjusted",
                target_type="customer",
                target_id=customer.id,
                diff_json={
                    "points_delta": body.points_delta,
                    "balance_after": txn.balance_after,
                    "reason": body.reason,
                    "actor_role": user.role,
                },
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if namespaced_key and _is_adjustment_idempotency_violation(exc):
            existing = await _find_by_idempotency_key(db, user.business_id, namespaced_key)
            if existing is not None:
                return LoyaltyAdjustmentResponse(
                    customer_id=existing.customer_id,
                    points_delta=existing.points_delta,
                    balance_after=existing.balance_after,
                    reason=existing.reason,
                )
        raise

    return LoyaltyAdjustmentResponse(
        customer_id=customer.id,
        points_delta=txn.points_delta,
        balance_after=txn.balance_after,
        reason=txn.reason,
    )
