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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_assets import asset_public_url
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.ownership import require_owned
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import (
    build_movement,
    is_idempotency_violation,
    wastage_idempotency_key,
    wastage_reversal_idempotency_key,
)
from backend.app.inventory.units import UnitError, format_quantity, to_milli, value_cents
from shared.enums import (
    CanonicalUnit,
    StockMovementType,
    StockSourceType,
    WastageReason,
)
from shared.models.asset import Asset
from shared.models.business import Business
from shared.models.inventory import Ingredient, StockMovement
from shared.models.order import Order
from shared.models.user import BusinessUser
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


async def _require_inventory_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    """
    Same entitlement gate record_wastage already applies inline — factored
    out here for the two routes below that were missing it (list and
    approve). Also returns the Business row so callers can read its current
    wastage_approval_threshold_cents (list/approve derive "did this need
    approval" from the CURRENT threshold — the threshold isn't persisted
    per-entry, so a threshold change after the fact changes how old entries
    are labelled; documented here rather than silently assumed).
    """
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.inventory_enabled:
        raise AppError(
            "INVENTORY_NOT_ENABLED",
            "The inventory module is not enabled for this business",
            409,
        )
    return business


async def _staff_name_map(
    db: AsyncSession, business_id: uuid.UUID, user_ids: set
) -> dict:
    """
    Tenant-scoped, null-safe display-name lookup for a batch of
    business_user ids. WastageEntry.created_by_user_id/approved_by_user_id
    deliberately have no FK to business_users (same pattern as
    Order.cancelled_by_user_id) — a permanently-deleted staff member's past
    entries must keep their (now orphaned) id and simply show no name.
    """
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(BusinessUser.id, BusinessUser.staff_name).where(
                BusinessUser.business_id == business_id,
                BusinessUser.id.in_(ids),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _photo_url_map(db: AsyncSession, business_id: uuid.UUID, asset_ids: set) -> dict:
    """
    Tenant-scoped, batched resolution of photo_asset_id -> a displayable
    URL, same pattern as routes_menu.py's image_asset_id resolution.
    Skips the query entirely when nothing needs resolving (the common case:
    most wastage entries carry no photo), so this never adds an extra round
    trip to the hot path.
    """
    ids = {a for a in asset_ids if a is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Asset.id, Asset.storage_path).where(
                Asset.business_id == business_id,
                Asset.id.in_(ids),
            )
        )
    ).all()
    return {row[0]: asset_public_url(row[1]) for row in rows}


def _wastage_status(entry: WastageEntry, requires_approval: bool) -> str:
    """
    One-word summary of where an entry sits in the review lifecycle.
    REJECTED and APPROVED are mutually exclusive in application logic (see
    reject_wastage/approve_wastage); PENDING_APPROVAL only applies while
    neither has happened yet AND the entry actually needs sign-off.
    """
    if entry.rejected_by_user_id is not None:
        return "REJECTED"
    if entry.approved_by_user_id is not None:
        return "APPROVED"
    if requires_approval:
        return "PENDING_APPROVAL"
    return "NOT_REQUIRED"


def _wastage_response(
    entry: WastageEntry,
    ingredient: Ingredient,
    threshold: int,
    names: dict,
    photo_urls: dict,
) -> "WastageResponse":
    """Single builder for the response shape, used by every route below so
    create/list/approve/reject can never drift out of sync with each other."""
    requires_approval = bool(threshold) and int(entry.value_cents or 0) >= threshold
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
        requires_approval=requires_approval,
        approved_by_user_id=entry.approved_by_user_id,
        rejected_by_user_id=entry.rejected_by_user_id,
        rejection_reason=entry.rejection_reason,
        rejected_at=entry.rejected_at,
        status=_wastage_status(entry, requires_approval),
        logged_by_name=names.get(entry.created_by_user_id),
        approved_by_name=names.get(entry.approved_by_user_id),
        rejected_by_name=names.get(entry.rejected_by_user_id),
        related_order_id=entry.related_order_id,
        photo_asset_id=entry.photo_asset_id,
        photo_url=photo_urls.get(entry.photo_asset_id),
        occurred_at=entry.occurred_at,
    )


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
    rejected_by_user_id: uuid.UUID | None = None
    rejection_reason: str | None = None
    rejected_at: datetime | None = None
    status: str = "NOT_REQUIRED"
    logged_by_name: str | None = None
    approved_by_name: str | None = None
    rejected_by_name: str | None = None
    related_order_id: uuid.UUID | None = None
    photo_asset_id: uuid.UUID | None = None
    photo_url: str | None = None
    occurred_at: datetime


class WastageRejectRequest(BaseModel):
    reason: str = Field(
        min_length=3, max_length=500,
        description="Why this entry is being rejected — required, distinct "
                    "from the entry's own loss reason code",
    )


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
    """
    Record a loss and post the matching negative stock movement.

    A STAFF entry valued at or above the approval threshold is still SAVED
    (with approved_by_user_id left null, i.e. pending) — it is never
    rejected. The food/stock is already physically gone by the time this is
    called, so refusing to record it would make a real loss silently
    disappear from the ledger, which is worse than an unapproved entry. The
    stock movement posts immediately either way so stock-on-hand is always
    correct; approval above threshold is a review/sign-off step afterwards
    (see approve_wastage), never a second deduction.
    """
    business = await _require_inventory_enabled(db, user.business_id)

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

    if body.photo_asset_id is not None:
        await require_owned(db, Asset, body.photo_asset_id, user.business_id, label="Asset")
    if body.related_order_id is not None:
        await require_owned(db, Order, body.related_order_id, user.business_id, label="Order")

    unit_cost = int(ingredient.cost_per_canonical_micro_cents or 0)
    loss_value = value_cents(quantity_milli, unit_cost)

    threshold = int(business.wastage_approval_threshold_cents or 0)
    is_manager = user.role in ("OWNER", "MANAGER")
    requires_approval = bool(threshold) and loss_value >= threshold
    # A STAFF entry above threshold is saved pending approval (see docstring)
    # rather than rejected. OWNER/MANAGER entries above threshold are
    # auto-approved by the person who logged them.

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
            idempotency_key=wastage_idempotency_key(entry.id, ingredient.id),
            notes=f"{body.reason}: {body.note}" if body.note else body.reason,
            occurred_at=occurred,
        )
    )
    await db.commit()
    await db.refresh(entry)

    names = await _staff_name_map(
        db, user.business_id,
        {entry.created_by_user_id, entry.approved_by_user_id, entry.rejected_by_user_id},
    )
    photo_urls = await _photo_url_map(db, user.business_id, {entry.photo_asset_id})

    return _wastage_response(entry, ingredient, threshold, names, photo_urls)


@router.get("", response_model=list[WastageResponse])
async def list_wastage(
    limit: int = Query(100, ge=1, le=500),
    since: datetime | None = Query(None),
    pending_only: bool = Query(
        False,
        description="Only entries awaiting manager review — value at/above "
                    "the business's approval threshold, neither approved "
                    "nor rejected yet. The review queue view.",
    ),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Recent wastage, newest first."""
    business = await _require_inventory_enabled(db, user.business_id)
    threshold = int(business.wastage_approval_threshold_cents or 0)

    if pending_only and not threshold:
        # Nothing can be "pending" when the business has no approval
        # threshold configured at all — every entry is NOT_REQUIRED, so an
        # unfiltered query would return everything, which would be
        # misleading for a screen titled "awaiting approval".
        return []

    stmt = (
        select(WastageEntry, Ingredient)
        .join(Ingredient, Ingredient.id == WastageEntry.ingredient_id)
        .where(WastageEntry.business_id == user.business_id)
        .order_by(WastageEntry.occurred_at.desc())
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(WastageEntry.occurred_at >= since)
    if pending_only:
        stmt = stmt.where(
            WastageEntry.approved_by_user_id.is_(None),
            WastageEntry.rejected_by_user_id.is_(None),
            WastageEntry.value_cents >= threshold,
        )

    rows = (await db.execute(stmt)).all()

    # One batched tenant-scoped name/photo lookup for every entry on this
    # page, instead of a query per row.
    name_ids = set()
    photo_ids = set()
    for entry, _ in rows:
        name_ids.add(entry.created_by_user_id)
        name_ids.add(entry.approved_by_user_id)
        name_ids.add(entry.rejected_by_user_id)
        photo_ids.add(entry.photo_asset_id)
    names = await _staff_name_map(db, user.business_id, name_ids)
    photo_urls = await _photo_url_map(db, user.business_id, photo_ids)

    return [
        _wastage_response(entry, ingredient, threshold, names, photo_urls)
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

    Idempotent: once an entry has an approver, a repeat call is a no-op
    that returns the same state rather than reassigning the approver (or
    writing a second time) — a double-tapped "approve" button can never
    change who signed off or touch the ledger again.
    """
    business = await _require_inventory_enabled(db, user.business_id)
    threshold = int(business.wastage_approval_threshold_cents or 0)
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

    if entry.rejected_by_user_id is not None:
        raise AppError(
            "WASTAGE_ALREADY_REJECTED",
            "This entry was rejected and cannot be approved. "
            "Log a fresh entry if the loss should be recorded after all.",
            409,
        )

    if entry.approved_by_user_id is None:
        entry.approved_by_user_id = user.user_id
        await db.commit()
        await db.refresh(entry)

    names = await _staff_name_map(
        db, user.business_id,
        {entry.created_by_user_id, entry.approved_by_user_id, entry.rejected_by_user_id},
    )
    photo_urls = await _photo_url_map(db, user.business_id, {entry.photo_asset_id})

    return _wastage_response(entry, ingredient, threshold, names, photo_urls)


@router.post("/{entry_id}/reject", response_model=WastageResponse)
async def reject_wastage(
    entry_id: uuid.UUID,
    body: WastageRejectRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a wastage entry a manager disagrees with, reversing its stock
    movement.

    WHY A REVERSAL, NOT A NO-OP
    ----------------------------
    record_wastage posts the stock deduction at SUBMISSION time, not at
    approval time — the loss is already physically gone the moment it's
    logged, so the ledger has to reflect that immediately regardless of
    whether a manager later signs off (see record_wastage's own docstring).
    Rejecting therefore means putting the stock back, not skipping a
    deduction that was never made.

    INVARIANT: EXACTLY ONE REVERSAL, EVER
    --------------------------------------
    The reversal is keyed with wastage_reversal_idempotency_key(entry_id,
    ingredient_id), paired with the same partial unique index on
    (business_id, idempotency_key) that already guarantees "at most one
    movement" for a sale, a stocktake adjustment, and the original wastage
    deduction itself. That is the enforcement mechanism — not application
    bookkeeping — so a genuine race between two reject calls can post the
    reversal at most once even if both pass the in-memory checks below.

    An entry that has already been approved cannot be rejected (approval is
    a closed, signed-off record; use a manual adjustment to correct it
    instead). Rejecting an already-rejected entry is a no-op that returns
    the same state without posting a second reversal.
    """
    business = await _require_inventory_enabled(db, user.business_id)
    threshold = int(business.wastage_approval_threshold_cents or 0)

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

    if entry.approved_by_user_id is not None:
        raise AppError(
            "WASTAGE_ALREADY_APPROVED",
            "This entry was already approved and cannot be rejected. "
            "Use a manual stock adjustment to correct it instead.",
            409,
        )

    if entry.rejected_by_user_id is None:
        original = (
            await db.execute(
                select(StockMovement)
                .where(
                    StockMovement.business_id == user.business_id,
                    StockMovement.source_type == StockSourceType.WASTAGE_ENTRY.value,
                    StockMovement.source_id == entry.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if original is None:
            # Should be unreachable — record_wastage always posts exactly
            # one movement per entry (see its own docstring) — but refuse
            # rather than mark this entry rejected with no matching stock
            # reversal, which would leave the ledger and the entry's status
            # disagreeing with each other.
            raise AppError(
                "WASTAGE_MOVEMENT_MISSING",
                "No stock movement was found for this entry, so it cannot "
                "be safely rejected. Contact support.",
                409,
            )

        entry.rejected_by_user_id = user.user_id
        entry.rejected_at = datetime.now(timezone.utc)
        entry.rejection_reason = body.reason
        db.add(
            build_movement(
                business_id=user.business_id,
                ingredient_id=entry.ingredient_id,
                quantity_milli=-int(original.quantity_milli),  # negate the original
                movement_type=StockMovementType.WASTAGE_REVERSAL.value,
                unit_cost_micro_cents=int(original.unit_cost_micro_cents or 0),
                source_type=StockSourceType.WASTAGE_ENTRY.value,
                source_id=entry.id,
                created_by_user_id=user.user_id,
                idempotency_key=wastage_reversal_idempotency_key(entry.id, entry.ingredient_id),
                notes=f"Rejected: {body.reason}",
            )
        )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if not is_idempotency_violation(exc):
                raise
            # A concurrent reject call already won the race and committed
            # its reversal — reload and return that committed state rather
            # than erroring, so a retried request is genuinely idempotent.
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
            entry, ingredient = row
        else:
            await db.refresh(entry)

    names = await _staff_name_map(
        db, user.business_id,
        {entry.created_by_user_id, entry.approved_by_user_id, entry.rejected_by_user_id},
    )
    photo_urls = await _photo_url_map(db, user.business_id, {entry.photo_asset_id})

    return _wastage_response(entry, ingredient, threshold, names, photo_urls)
