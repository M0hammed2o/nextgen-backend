"""
Wastage routes — fast, reason-coded capture of stock loss.

Deliberately available to ANY staff member, because speed is the whole
design constraint. If logging a dropped dough ball is slower than throwing
it away, it will not be logged, and unrecorded wastage is mathematically
indistinguishable from theft in a variance report. A control that causes the
data to go missing is worse than no control.

Manager approval is therefore threshold-based (Business.
wastage_approval_threshold_cents) rather than mandatory: small everyday
losses record instantly, and only material ones need sign-off.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import build_movement
from backend.app.inventory.units import UnitError, format_quantity, to_milli, value_cents
from shared.enums import (
    CanonicalUnit,
    StockMovementType,
    StockSourceType,
    WastageReason,
)
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.wastage import WastageEntry

router = APIRouter(prefix="/business/wastage", tags=["wastage"])

# Reasons that are a cost of doing business rather than a process failure.
# They post their own movement types so reporting can separate genuine waste
# from meals the business chose to give away.
_MOVEMENT_TYPE_FOR_REASON = {
    WastageReason.STAFF_MEAL.value: StockMovementType.STAFF_MEAL.value,
    WastageReason.COMPLIMENTARY.value: StockMovementType.COMPLIMENTARY.value,
}

_REASON_PATTERN = "^(" + "|".join(r.value for r in WastageReason) + ")$"


class WastageCreateRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str = Field(description="Positive magnitude wasted")
    unit: str = Field(max_length=32)
    reason: str = Field(pattern=_REASON_PATTERN)
    note: str | None = Field(default=None, max_length=1000)
    related_order_id: uuid.UUID | None = None
    photo_asset_id: uuid.UUID | None = None
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def require_note_for_other(self) -> "WastageCreateRequest":
        # An unexplained "other" is exactly the unattributable variance this
        # whole subsystem exists to eliminate, so it must carry an explanation.
        if self.reason == WastageReason.OTHER.value and not (self.note or "").strip():
            raise ValueError("A note is required when the reason is OTHER")
        return self


class WastageResponse(BaseModel):
    id: uuid.UUID
    ingredient_id: uuid.UUID
    ingredient_name: str
    quantity_milli: int
    quantity_display: str
    reason: str
    note: str | None
    value_cents: int
    requires_approval: bool
    approved_by_user_id: uuid.UUID | None
    occurred_at: datetime


@router.get("/reasons")
async def list_reasons(user: AuthUser = Depends(require_staff_or_above)):
    """
    The reason codes the till should offer, in the order they should appear.

    Served from the API rather than hardcoded in the PWA so the list stays in
    step with the enum the ledger actually validates against.
    """
    labels = {
        WastageReason.DROPPED.value: "Dropped",
        WastageReason.BURNT.value: "Burnt",
        WastageReason.INCORRECT_PREPARATION.value: "Made wrong",
        WastageReason.REMAKE.value: "Customer remake",
        WastageReason.SPOILED.value: "Spoiled",
        WastageReason.EXPIRED.value: "Expired",
        WastageReason.DAMAGED_PACKAGING.value: "Damaged packaging",
        WastageReason.STAFF_MEAL.value: "Staff meal",
        WastageReason.COMPLIMENTARY.value: "Complimentary",
        WastageReason.TRAINING.value: "Training",
        WastageReason.OTHER.value: "Other",
    }
    return [
        {
            "value": value,
            "label": label,
            "requires_note": value == WastageReason.OTHER.value,
        }
        for value, label in labels.items()
    ]


@router.post("", response_model=WastageResponse, status_code=201)
async def record_wastage(
    body: WastageCreateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Record a loss and post the matching negative stock movement."""
    business = (
        await db.execute(select(Business).where(Business.id == user.business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.inventory_enabled:
        raise AppError(
            "INVENTORY_NOT_ENABLED",
            "The inventory module is not enabled for this business",
            409,
        )

    ingredient = (
        await db.execute(
            select(Ingredient).where(
                Ingredient.id == body.ingredient_id,
                Ingredient.business_id == user.business_id,
                Ingredient.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not ingredient:
        raise NotFoundError("Ingredient", str(body.ingredient_id))

    try:
        quantity_milli = to_milli(
            body.quantity, body.unit, canonical=CanonicalUnit(ingredient.canonical_unit)
        )
    except (UnitError, ValueError) as exc:
        raise AppError("INVALID_UNIT", str(exc), 422) from exc
    if quantity_milli <= 0:
        raise AppError("INVALID_QUANTITY", "Wasted quantity must be greater than zero", 422)

    unit_cost = int(ingredient.cost_per_canonical_micro_cents or 0)
    loss_value = value_cents(quantity_milli, unit_cost)

    threshold = int(business.wastage_approval_threshold_cents or 0)
    is_manager = user.role in ("OWNER", "MANAGER")
    requires_approval = bool(threshold) and loss_value >= threshold

    if requires_approval and not is_manager:
        raise AppError(
            "WASTAGE_APPROVAL_REQUIRED",
            f"Wastage valued at {loss_value} cents needs manager approval "
            f"(threshold is {threshold} cents)",
            403,
        )

    occurred = body.occurred_at or datetime.now(timezone.utc)
    entry = WastageEntry(
        business_id=user.business_id,
        ingredient_id=ingredient.id,
        quantity_milli=quantity_milli,
        entered_quantity=str(body.quantity),
        entered_unit=body.unit,
        reason=body.reason,
        note=body.note,
        value_cents=loss_value,
        created_by_user_id=user.user_id,
        approved_by_user_id=user.user_id if (requires_approval and is_manager) else None,
        photo_asset_id=body.photo_asset_id,
        related_order_id=body.related_order_id,
        occurred_at=occurred,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    await db.flush()

    db.add(
        build_movement(
            business_id=user.business_id,
            ingredient_id=ingredient.id,
            quantity_milli=-quantity_milli,  # wastage removes stock
            movement_type=_MOVEMENT_TYPE_FOR_REASON.get(
                body.reason, StockMovementType.WASTAGE.value
            ),
            unit_cost_micro_cents=unit_cost,
            source_type=StockSourceType.WASTAGE_ENTRY.value,
            source_id=entry.id,
            created_by_user_id=user.user_id,
            notes=f"{body.reason}: {body.note}" if body.note else body.reason,
            occurred_at=occurred,
        )
    )
    await db.commit()
    await db.refresh(entry)

    return WastageResponse(
        id=entry.id,
        ingredient_id=ingredient.id,
        ingredient_name=ingredient.name,
        quantity_milli=quantity_milli,
        quantity_display=format_quantity(
            quantity_milli, CanonicalUnit(ingredient.canonical_unit), ingredient.unit_label
        ),
        reason=entry.reason,
        note=entry.note,
        value_cents=loss_value,
        requires_approval=requires_approval,
        approved_by_user_id=entry.approved_by_user_id,
        occurred_at=entry.occurred_at,
    )


@router.get("", response_model=list[WastageResponse])
async def list_wastage(
    limit: int = Query(100, ge=1, le=500),
    since: datetime | None = Query(None),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Recent wastage, newest first."""
    stmt = (
        select(WastageEntry, Ingredient)
        .join(Ingredient, Ingredient.id == WastageEntry.ingredient_id)
        .where(WastageEntry.business_id == user.business_id)
        .order_by(WastageEntry.occurred_at.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(WastageEntry.occurred_at >= since)

    rows = (await db.execute(stmt)).all()
    return [
        WastageResponse(
            id=entry.id,
            ingredient_id=entry.ingredient_id,
            ingredient_name=ingredient.name,
            quantity_milli=int(entry.quantity_milli),
            quantity_display=format_quantity(
                int(entry.quantity_milli), CanonicalUnit(ingredient.canonical_unit),
                ingredient.unit_label
            ),
            reason=entry.reason,
            note=entry.note,
            value_cents=int(entry.value_cents or 0),
            requires_approval=entry.approved_by_user_id is not None,
            approved_by_user_id=entry.approved_by_user_id,
            occurred_at=entry.occurred_at,
        )
        for entry, ingredient in rows
    ]


@router.post("/{entry_id}/approve", response_model=WastageResponse)
async def approve_wastage(
    entry_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Sign off a wastage entry after the fact.

    Note this does NOT re-post a stock movement — the loss was already
    recorded when it happened. Approval is a control record, not a second
    deduction; treating it as one would double-count the loss.
    """
    row = (
        await db.execute(
            select(WastageEntry, Ingredient)
            .join(Ingredient, Ingredient.id == WastageEntry.ingredient_id)
            .where(
                WastageEntry.id == entry_id,
                WastageEntry.business_id == user.business_id,
            )
        )
    ).first()
    if not row:
        raise NotFoundError("Wastage entry", str(entry_id))
    entry, ingredient = row

    entry.approved_by_user_id = user.user_id
    await db.commit()
    await db.refresh(entry)

    return WastageResponse(
        id=entry.id,
        ingredient_id=entry.ingredient_id,
        ingredient_name=ingredient.name,
        quantity_milli=int(entry.quantity_milli),
        quantity_display=format_quantity(
            int(entry.quantity_milli), CanonicalUnit(ingredient.canonical_unit),
            ingredient.unit_label
        ),
        reason=entry.reason,
        note=entry.note,
        value_cents=int(entry.value_cents or 0),
        requires_approval=True,
        approved_by_user_id=entry.approved_by_user_id,
        occurred_at=entry.occurred_at,
    )
