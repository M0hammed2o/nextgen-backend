"""
Loyalty Programme routes — program configuration, reward catalogue, and
staff-facing customer lookup/redemption for the POS.

Program config and reward catalogue are OWNER/MANAGER-only (business
decisions about earning/redemption rules); looking up a customer's balance
and redeeming a reward are STAFF-or-above (till-floor actions).
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.loyalty.engine import get_or_create_customer_by_phone, redeem_reward
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyProgram, LoyaltyReward

router = APIRouter(prefix="/business/loyalty", tags=["loyalty"])


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


class LoyaltyRewardUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    points_cost: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class LoyaltyRewardResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    points_cost: int
    is_active: bool
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


class RedeemResponse(BaseModel):
    success: bool
    points_delta: int
    balance_after: int


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
    program = await _get_or_create_program(db, user.business_id)
    return LoyaltyProgramResponse.model_validate(program)


@router.put("/program", response_model=LoyaltyProgramResponse)
async def update_loyalty_program(
    body: LoyaltyProgramUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
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

    txn = await redeem_reward(db, user.business_id, customer, reward, program)
    await db.commit()

    return RedeemResponse(success=True, points_delta=txn.points_delta, balance_after=txn.balance_after)
