"""
Stocktake routes — the physical count, its variance, and manager sign-off.

LIFECYCLE (one-way, modelled on TillSession)

    DRAFT ---> COMPLETED ---> APPROVED
      |            |
      +--> CANCELLED

An APPROVED count is immutable. Every mutating endpoint checks this before
touching anything, exactly as the till refuses to reopen a CLOSED cash-up.

THE CUT-OFF
-----------
A session fixes `cutoff_at` when it opens. Expected quantities count only
ledger movements with occurred_at <= cutoff_at, so a sale rung up while staff
are still counting falls into the NEXT period instead of silently corrupting
this one. `period_start_at` is the previous approved count's cut-off, so
consecutive periods tile exactly with no gap and no overlap.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import (
    build_movement,
    movement_totals_by_type,
    stock_on_hand,
    stocktake_adjustment_idempotency_key,
)
from backend.app.inventory.units import UnitError, format_quantity, to_milli, value_cents
from backend.app.inventory.variance import (
    DEFAULT_VARIANCE_ALERT_BASIS_POINTS,
    IngredientVariance,
    format_basis_points,
    value_weighted_accuracy_basis_points,
)
from shared.enums import (
    AuditScope,
    CanonicalUnit,
    StockMovementType,
    StockSourceType,
    StocktakeStatus,
)
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.stocktake import StocktakeLine, StocktakeSession
from shared.models.user import BusinessUser

router = APIRouter(prefix="/business/stocktake", tags=["stocktake"])

# Which movement types feed which term of the variance equation.
_RECEIPT_TYPES = {StockMovementType.GOODS_RECEIVED.value}
_CONSUMPTION_TYPES = {
    StockMovementType.SALE_CONSUMPTION.value,
    StockMovementType.REMAKE_CONSUMPTION.value,
    StockMovementType.SALE_REVERSAL.value,
}
_WASTAGE_TYPES = {
    StockMovementType.WASTAGE.value,
    StockMovementType.STAFF_MEAL.value,
    StockMovementType.COMPLIMENTARY.value,
}


async def _require_inventory_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
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


async def _load_session(
    db: AsyncSession,
    business_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> StocktakeSession:
    """
    `for_update` locks the StocktakeSession row (SELECT ... FOR UPDATE) so a
    double-submitted approval serializes: the second caller blocks until the
    first commits, then re-reads status == APPROVED and is rejected by the
    checks in approve_stocktake — see stocktake_adjustment_idempotency_key()
    for the belt-and-braces DB-level guarantee behind that same check.
    """
    stmt = (
        select(StocktakeSession)
        .options(selectinload(StocktakeSession.lines))
        .where(
            StocktakeSession.id == session_id,
            StocktakeSession.business_id == business_id,
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise NotFoundError("Stocktake", str(session_id))
    return session


def _reject_if_locked(session: StocktakeSession) -> None:
    if session.status == StocktakeStatus.APPROVED.value:
        raise AppError(
            "STOCKTAKE_LOCKED",
            "This stocktake was approved and can no longer be changed",
            409,
        )
    if session.status == StocktakeStatus.CANCELLED.value:
        raise AppError("STOCKTAKE_CANCELLED", "This stocktake was cancelled", 409)


# ── Schemas ──────────────────────────────────────────────────────────────────

class StocktakeOpenRequest(BaseModel):
    reference: str | None = Field(default=None, max_length=64)
    cutoff_at: datetime | None = Field(
        default=None, description="Defaults to now — the instant the count represents"
    )
    notes: str | None = None


class CountLineRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str
    # Required — a count submitted without a unit used to be silently
    # interpreted in the ingredient's canonical unit (grams/millilitres/each),
    # which is wrong whenever staff count in kg, L, or a pack/portion unit.
    # See KNOWN_BUGS.md BUG-0047.
    unit: str = Field(min_length=1, max_length=32)
    notes: str | None = None


class CountSubmitRequest(BaseModel):
    lines: list[CountLineRequest] = Field(min_length=1)


class StocktakeLineResponse(BaseModel):
    ingredient_id: uuid.UUID
    ingredient_name: str
    canonical_unit: str
    count_method: str
    unit_label: str | None = None
    counted_quantity_milli: int | None
    counted_display: str | None
    entered_quantity: str | None = None
    entered_unit: str | None = None
    opening_quantity_milli: int
    received_quantity_milli: int
    theoretical_consumption_milli: int
    wastage_quantity_milli: int
    adjustment_quantity_milli: int
    expected_quantity_milli: int
    expected_display: str
    variance_quantity_milli: int
    variance_display: str
    variance_value_cents: int
    variance_basis_points: int | None
    variance_percent: str
    is_flagged: bool
    notes: str | None


class StocktakeResponse(BaseModel):
    id: uuid.UUID
    reference: str | None
    status: str
    cutoff_at: datetime
    period_start_at: datetime | None
    opened_at: datetime
    completed_at: datetime | None
    approved_at: datetime | None
    opened_by_user_id: uuid.UUID | None
    completed_by_user_id: uuid.UUID | None
    approved_by_user_id: uuid.UUID | None
    total_variance_value_cents: int
    total_theoretical_value_cents: int
    accuracy_basis_points: int | None
    accuracy_percent: str
    is_locked: bool
    lines: list[StocktakeLineResponse]
    notes: str | None


# ── Building the variance view ───────────────────────────────────────────────

async def _compute_variances(
    db: AsyncSession,
    business_id: uuid.UUID,
    session: StocktakeSession,
    ingredients: dict[uuid.UUID, Ingredient],
) -> dict[uuid.UUID, IngredientVariance]:
    """
    Rebuild each ingredient's variance terms from the ledger.

    Opening comes from the position at the PREVIOUS period's cut-off, and
    every other term from movements strictly after it and up to this
    session's cut-off — so periods tile exactly.
    """
    ingredient_ids = list(ingredients.keys())
    opening = (
        await stock_on_hand(
            db, business_id, as_of=session.period_start_at, ingredient_ids=ingredient_ids
        )
        if session.period_start_at
        else {}
    )
    totals = await movement_totals_by_type(
        db,
        business_id,
        since=session.period_start_at,
        until=session.cutoff_at,
        ingredient_ids=ingredient_ids,
    )

    out: dict[uuid.UUID, IngredientVariance] = {}
    for ingredient_id, ingredient in ingredients.items():
        received = consumption = wastage = adjustment = 0
        for (row_ingredient, movement_type), quantity in totals.items():
            if row_ingredient != ingredient_id:
                continue
            if movement_type in _RECEIPT_TYPES:
                received += quantity
            elif movement_type in _CONSUMPTION_TYPES:
                # Stored negative; the equation wants a positive magnitude.
                consumption += -quantity
            elif movement_type in _WASTAGE_TYPES:
                wastage += -quantity
            else:
                adjustment += quantity

        out[ingredient_id] = IngredientVariance(
            opening_milli=opening.get(ingredient_id, 0),
            received_milli=received,
            theoretical_consumption_milli=consumption,
            wastage_milli=wastage,
            adjustment_milli=adjustment,
            counted_milli=0,
            unit_cost_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
        )
    return out


def _line_response(
    line: StocktakeLine, ingredient: Ingredient, variance: IngredientVariance
) -> StocktakeLineResponse:
    unit = CanonicalUnit(ingredient.canonical_unit)
    label = ingredient.unit_label
    return StocktakeLineResponse(
        ingredient_id=ingredient.id,
        ingredient_name=ingredient.name,
        canonical_unit=ingredient.canonical_unit,
        count_method=line.count_method_snapshot or ingredient.count_method,
        unit_label=label,
        counted_quantity_milli=line.counted_quantity_milli,
        counted_display=(
            format_quantity(line.counted_quantity_milli, unit, label)
            if line.counted_quantity_milli is not None
            else None
        ),
        entered_quantity=line.entered_quantity,
        entered_unit=line.entered_unit,
        opening_quantity_milli=variance.opening_milli,
        received_quantity_milli=variance.received_milli,
        theoretical_consumption_milli=variance.theoretical_consumption_milli,
        wastage_quantity_milli=variance.wastage_milli,
        adjustment_quantity_milli=variance.adjustment_milli,
        expected_quantity_milli=variance.expected_milli,
        expected_display=format_quantity(variance.expected_milli, unit, label),
        variance_quantity_milli=variance.variance_milli,
        variance_display=format_quantity(variance.variance_milli, unit, label),
        variance_value_cents=variance.variance_value_cents,
        variance_basis_points=variance.variance_basis_points,
        variance_percent=format_basis_points(variance.variance_basis_points),
        is_flagged=variance.is_flagged,
        notes=line.notes,
    )


async def _build_response(
    db: AsyncSession, business_id: uuid.UUID, session: StocktakeSession
) -> StocktakeResponse:
    ingredient_ids = [line.ingredient_id for line in (session.lines or [])]
    ingredients = {
        i.id: i
        for i in (
            (
                await db.execute(
                    select(Ingredient).where(
                        Ingredient.business_id == business_id,
                        Ingredient.id.in_(ingredient_ids or [uuid.uuid4()]),
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    if session.status == StocktakeStatus.APPROVED.value:
        # Locked: replay exactly the figures that were signed off, never a
        # fresh computation — later corrections must not rewrite a signed
        # variance report.
        variances = {
            line.ingredient_id: IngredientVariance(
                opening_milli=int(line.opening_quantity_milli or 0),
                received_milli=int(line.received_quantity_milli or 0),
                theoretical_consumption_milli=int(line.theoretical_consumption_milli or 0),
                wastage_milli=int(line.wastage_quantity_milli or 0),
                adjustment_milli=int(line.adjustment_quantity_milli or 0),
                counted_milli=int(line.counted_quantity_milli or 0),
                unit_cost_micro_cents=int(line.unit_cost_micro_cents or 0),
            )
            for line in (session.lines or [])
        }
    else:
        variances = await _compute_variances(db, business_id, session, ingredients)
        for line in session.lines or []:
            if line.ingredient_id in variances and line.counted_quantity_milli is not None:
                variances[line.ingredient_id].counted_milli = int(line.counted_quantity_milli)

    line_responses = [
        _line_response(line, ingredients[line.ingredient_id], variances[line.ingredient_id])
        for line in sorted(
            session.lines or [],
            key=lambda x: ingredients.get(x.ingredient_id).name if ingredients.get(x.ingredient_id) else "",
        )
        if line.ingredient_id in ingredients and line.ingredient_id in variances
    ]
    # Worst rand exposure first — a manager should never have to hunt for the
    # problem that is actually costing money.
    line_responses.sort(key=lambda x: abs(x.variance_value_cents), reverse=True)

    counted = [
        variances[line.ingredient_id]
        for line in (session.lines or [])
        if line.counted_quantity_milli is not None and line.ingredient_id in variances
    ]
    accuracy_bp = value_weighted_accuracy_basis_points(counted) if counted else None

    return StocktakeResponse(
        id=session.id,
        reference=session.reference,
        status=session.status,
        cutoff_at=session.cutoff_at,
        period_start_at=session.period_start_at,
        opened_at=session.opened_at,
        completed_at=session.completed_at,
        approved_at=session.approved_at,
        opened_by_user_id=session.opened_by_user_id,
        completed_by_user_id=session.completed_by_user_id,
        approved_by_user_id=session.approved_by_user_id,
        total_variance_value_cents=(
            int(session.total_variance_value_cents or 0)
            if session.status == StocktakeStatus.APPROVED.value
            else sum(v.variance_value_cents for v in counted)
        ),
        total_theoretical_value_cents=(
            int(session.total_theoretical_value_cents or 0)
            if session.status == StocktakeStatus.APPROVED.value
            else sum(v.theoretical_value_cents for v in counted)
        ),
        accuracy_basis_points=accuracy_bp,
        accuracy_percent=format_basis_points(accuracy_bp),
        is_locked=session.is_locked,
        lines=line_responses,
        notes=session.notes,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/open", response_model=StocktakeResponse, status_code=201)
async def open_stocktake(
    body: StocktakeOpenRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Start a count. Creates one line per active ingredient, pre-loaded with the
    count method staff should use, and fixes the cut-off instant.
    """
    await _require_inventory_enabled(db, user.business_id)

    open_already = (
        await db.execute(
            select(StocktakeSession).where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status == StocktakeStatus.DRAFT.value,
            )
        )
    ).scalar_one_or_none()
    if open_already:
        raise AppError(
            "STOCKTAKE_ALREADY_OPEN",
            "A stocktake is already in progress — complete or cancel it first",
            409,
        )

    completed_unapproved = (
        await db.execute(
            select(StocktakeSession).where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status == StocktakeStatus.COMPLETED.value,
            )
        )
    ).scalar_one_or_none()
    if completed_unapproved:
        # A COMPLETED session still owns the period up to its cutoff_at — the
        # next session's period_start_at is only ever set from an APPROVED
        # count's cutoff (see `previous` below), so starting a new DRAFT here
        # would reuse the same period_start_at as the still-pending one and
        # silently produce two overlapping variance periods.
        raise AppError(
            "STOCKTAKE_PENDING_APPROVAL",
            "A completed stocktake is awaiting manager approval — approve or "
            "cancel it before starting a new count",
            409,
        )

    previous = (
        await db.execute(
            select(StocktakeSession)
            .where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status == StocktakeStatus.APPROVED.value,
            )
            .order_by(StocktakeSession.cutoff_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    session = StocktakeSession(
        business_id=user.business_id,
        reference=body.reference or f"Count {now.strftime('%Y-%m-%d %H:%M')}",
        status=StocktakeStatus.DRAFT.value,
        cutoff_at=body.cutoff_at or now,
        period_start_at=previous.cutoff_at if previous else None,
        opened_by_user_id=user.user_id,
        opened_at=now,
        notes=body.notes,
    )
    db.add(session)
    await db.flush()

    ingredients = (
        (
            await db.execute(
                select(Ingredient).where(
                    Ingredient.business_id == user.business_id,
                    Ingredient.is_active.is_(True),
                    Ingredient.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    if not ingredients:
        raise AppError(
            "NO_INGREDIENTS",
            "Add at least one ingredient before starting a stocktake",
            422,
        )

    for ingredient in ingredients:
        db.add(
            StocktakeLine(
                business_id=user.business_id,
                stocktake_session_id=session.id,
                ingredient_id=ingredient.id,
                count_method_snapshot=ingredient.count_method,
                unit_cost_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
            )
        )

    await db.commit()
    return await _build_response(db, user.business_id, await _load_session(db, user.business_id, session.id))


@router.get("/current", response_model=StocktakeResponse | None)
async def get_current_stocktake(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """The count in progress, if any — how a tablet resumes a saved draft."""
    await _require_inventory_enabled(db, user.business_id)
    session = (
        await db.execute(
            select(StocktakeSession)
            .options(selectinload(StocktakeSession.lines))
            .where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status.in_(
                    (StocktakeStatus.DRAFT.value, StocktakeStatus.COMPLETED.value)
                ),
            )
            .order_by(StocktakeSession.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not session:
        return None
    return await _build_response(db, user.business_id, session)


@router.get("", response_model=list[dict])
async def list_stocktakes(
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Historical counts, newest first. Approved counts are preserved forever.

    Carries enough to read the history without opening each count: when it was
    taken, the cut-off it was measured to, who took it, how many lines were
    actually counted, and the rolled-up difference. The per-ingredient
    expected/counted/variance detail lives on GET /{session_id}, which is the
    same snapshot frozen at completion — never recomputed, so a count read
    today shows what it showed the day it was signed off.
    """
    await _require_inventory_enabled(db, user.business_id)
    rows = (
        (
            await db.execute(
                select(StocktakeSession)
                .where(StocktakeSession.business_id == user.business_id)
                .order_by(StocktakeSession.opened_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Staff names in one query rather than per row.
    user_ids: set[uuid.UUID] = set()
    for s in rows:
        for uid in (s.opened_by_user_id, s.completed_by_user_id, s.approved_by_user_id):
            if uid:
                user_ids.add(uid)
    names: dict[uuid.UUID, str] = {}
    if user_ids:
        for bu in (
            (
                await db.execute(
                    select(BusinessUser).where(
                        BusinessUser.business_id == user.business_id,
                        BusinessUser.id.in_(user_ids),
                    )
                )
            )
            .scalars()
            .all()
        ):
            names[bu.id] = bu.staff_name or bu.email or "Unknown"

    # Line counts per session, again in one query.
    counted_by_session: dict[uuid.UUID, int] = {}
    total_by_session: dict[uuid.UUID, int] = {}
    if rows:
        session_ids = [s.id for s in rows]
        for sid, total, counted in (
            await db.execute(
                select(
                    StocktakeLine.stocktake_session_id,
                    func.count(StocktakeLine.id),
                    func.count(StocktakeLine.counted_quantity_milli),
                )
                .where(
                    StocktakeLine.business_id == user.business_id,
                    StocktakeLine.stocktake_session_id.in_(session_ids),
                )
                .group_by(StocktakeLine.stocktake_session_id)
            )
        ).all():
            total_by_session[sid] = int(total or 0)
            counted_by_session[sid] = int(counted or 0)

    return [
        {
            "id": str(s.id),
            "reference": s.reference,
            "status": s.status,
            "opened_at": s.opened_at.isoformat() if s.opened_at else None,
            "cutoff_at": s.cutoff_at.isoformat() if s.cutoff_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "approved_at": s.approved_at.isoformat() if s.approved_at else None,
            "opened_by_name": names.get(s.opened_by_user_id) if s.opened_by_user_id else None,
            "completed_by_name": (
                names.get(s.completed_by_user_id) if s.completed_by_user_id else None
            ),
            "approved_by_name": (
                names.get(s.approved_by_user_id) if s.approved_by_user_id else None
            ),
            "line_count": total_by_session.get(s.id, 0),
            "counted_line_count": counted_by_session.get(s.id, 0),
            "total_variance_value_cents": int(s.total_variance_value_cents or 0),
            "total_theoretical_value_cents": int(s.total_theoretical_value_cents or 0),
            "is_locked": s.is_locked,
            "notes": s.notes,
        }
        for s in rows
    ]


@router.get("/{session_id}", response_model=StocktakeResponse)
async def get_stocktake(
    session_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    session = await _load_session(db, user.business_id, session_id)
    return await _build_response(db, user.business_id, session)


@router.post("/{session_id}/counts", response_model=StocktakeResponse)
async def submit_counts(
    session_id: uuid.UUID,
    body: CountSubmitRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Save counted quantities. Safe to call repeatedly — this is the draft-save
    that lets staff count in passes and resume on another device.
    """
    await _require_inventory_enabled(db, user.business_id)
    session = await _load_session(db, user.business_id, session_id)
    _reject_if_locked(session)
    if session.status == StocktakeStatus.COMPLETED.value:
        # _reject_if_locked only blocks APPROVED/CANCELLED (cancel_stocktake
        # must still be able to act on a COMPLETED-unapproved session per the
        # lifecycle diagram above), but editing counts after COMPLETED would
        # let approval post a variance that no longer matches what was
        # actually snapshotted. There is no "reopen" transition in this
        # codebase today, so this is a hard lock, not a re-snapshot.
        raise AppError(
            "STOCKTAKE_COMPLETED",
            "This stocktake has already been completed and its variance snapshotted — "
            "counts can no longer be edited. Cancel it and open a new count if the "
            "figures were wrong.",
            409,
        )

    lines = {line.ingredient_id: line for line in (session.lines or [])}
    ingredients = {
        i.id: i
        for i in (
            (
                await db.execute(
                    select(Ingredient).where(
                        Ingredient.business_id == user.business_id,
                        Ingredient.id.in_([line.ingredient_id for line in body.lines]),
                    )
                )
            )
            .scalars()
            .all()
        )
    }

    for entry in body.lines:
        line = lines.get(entry.ingredient_id)
        ingredient = ingredients.get(entry.ingredient_id)
        if not line or not ingredient:
            raise AppError(
                "INGREDIENT_NOT_ON_COUNT",
                f"Ingredient {entry.ingredient_id} is not part of this stocktake",
                422,
            )
        unit = entry.unit
        try:
            counted = to_milli(
                entry.quantity, unit, canonical=CanonicalUnit(ingredient.canonical_unit)
            )
        except (UnitError, ValueError) as exc:
            raise AppError("INVALID_UNIT", f"{ingredient.name}: {exc}", 422) from exc
        if counted < 0:
            raise AppError(
                "INVALID_QUANTITY", f"{ingredient.name}: a count cannot be negative", 422
            )

        line.counted_quantity_milli = counted
        line.entered_quantity = str(entry.quantity)
        line.entered_unit = unit
        if entry.notes is not None:
            line.notes = entry.notes

    await db.commit()
    return await _build_response(db, user.business_id, await _load_session(db, user.business_id, session_id))


@router.post("/{session_id}/complete", response_model=StocktakeResponse)
async def complete_stocktake(
    session_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Freeze the count and snapshot every variance term onto its line.

    Snapshotting is what makes an approved variance reproducible forever:
    later compensating movements can never retroactively change the figures a
    manager signed off.
    """
    await _require_inventory_enabled(db, user.business_id)
    session = await _load_session(db, user.business_id, session_id)
    _reject_if_locked(session)
    if session.status != StocktakeStatus.DRAFT.value:
        raise AppError("STOCKTAKE_NOT_DRAFT", "Only a draft stocktake can be completed", 409)

    uncounted = [line for line in (session.lines or []) if line.counted_quantity_milli is None]
    if uncounted:
        raise AppError(
            "INCOMPLETE_COUNT",
            f"{len(uncounted)} ingredient(s) still have no counted quantity",
            422,
            details={"uncounted_ingredient_ids": [str(l.ingredient_id) for l in uncounted]},
        )

    ingredients = {
        i.id: i
        for i in (
            (
                await db.execute(
                    select(Ingredient).where(
                        Ingredient.business_id == user.business_id,
                        Ingredient.id.in_([line.ingredient_id for line in session.lines]),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    variances = await _compute_variances(db, user.business_id, session, ingredients)

    total_variance = 0
    total_theoretical = 0
    for line in session.lines or []:
        variance = variances.get(line.ingredient_id)
        if not variance:
            continue
        variance.counted_milli = int(line.counted_quantity_milli or 0)

        line.opening_quantity_milli = variance.opening_milli
        line.received_quantity_milli = variance.received_milli
        line.theoretical_consumption_milli = variance.theoretical_consumption_milli
        line.wastage_quantity_milli = variance.wastage_milli
        line.adjustment_quantity_milli = variance.adjustment_milli
        line.expected_quantity_milli = variance.expected_milli
        line.variance_quantity_milli = variance.variance_milli
        line.variance_value_cents = variance.variance_value_cents
        line.variance_basis_points = variance.variance_basis_points
        line.unit_cost_micro_cents = variance.unit_cost_micro_cents

        total_variance += variance.variance_value_cents
        total_theoretical += variance.theoretical_value_cents

    session.status = StocktakeStatus.COMPLETED.value
    session.completed_by_user_id = user.user_id
    session.completed_at = datetime.now(timezone.utc)
    session.total_variance_value_cents = total_variance
    session.total_theoretical_value_cents = total_theoretical

    await db.commit()
    return await _build_response(db, user.business_id, await _load_session(db, user.business_id, session_id))


@router.post("/{session_id}/approve", response_model=StocktakeResponse)
async def approve_stocktake(
    session_id: uuid.UUID,
    post_adjustments: bool = Query(
        True,
        description="Post STOCKTAKE_ADJUSTMENT movements so book stock matches the count",
    ),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Manager sign-off. Locks the count permanently and, by default, posts an
    adjustment per ingredient so book stock equals what was physically found.

    Without those adjustments the next period would inherit the same variance
    and double-count it. They are posted as explicit STOCKTAKE_ADJUSTMENT
    movements rather than by editing history — the ledger stays append-only.
    """
    await _require_inventory_enabled(db, user.business_id)
    # Row-locked: a double-clicked approve must not post adjustments twice.
    # See _load_session's docstring for how this pairs with the idempotency
    # key on each movement below.
    session = await _load_session(db, user.business_id, session_id, for_update=True)

    if session.status == StocktakeStatus.APPROVED.value:
        raise AppError("STOCKTAKE_LOCKED", "This stocktake is already approved", 409)
    if session.status != StocktakeStatus.COMPLETED.value:
        raise AppError(
            "STOCKTAKE_NOT_COMPLETED", "Complete the count before approving it", 409
        )

    now = datetime.now(timezone.utc)
    posted = 0
    if post_adjustments:
        for line in session.lines or []:
            delta = int(line.variance_quantity_milli or 0)
            if delta == 0:
                continue
            db.add(
                build_movement(
                    business_id=user.business_id,
                    ingredient_id=line.ingredient_id,
                    quantity_milli=delta,
                    movement_type=StockMovementType.STOCKTAKE_ADJUSTMENT.value,
                    unit_cost_micro_cents=int(line.unit_cost_micro_cents or 0),
                    source_type=StockSourceType.STOCKTAKE.value,
                    source_id=session.id,
                    created_by_user_id=user.user_id,
                    notes=f"Stocktake {session.reference} correction",
                    # Stamped AT the cut-off, not now, so the correction lands
                    # inside the period it belongs to and the next period
                    # opens from a clean, reconciled position.
                    occurred_at=session.cutoff_at,
                    idempotency_key=stocktake_adjustment_idempotency_key(
                        session.id, line.ingredient_id
                    ),
                )
            )
            posted += 1

    session.status = StocktakeStatus.APPROVED.value
    session.approved_by_user_id = user.user_id
    session.approved_at = now

    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="inventory.stocktake.approved",
            target_type="stocktake",
            target_id=session.id,
            diff_json={
                "reference": session.reference,
                "total_variance_value_cents": int(session.total_variance_value_cents or 0),
                "adjustments_posted": posted,
                "actor_role": user.role,
            },
        )
    )
    await db.commit()
    return await _build_response(db, user.business_id, await _load_session(db, user.business_id, session_id))


@router.post("/{session_id}/cancel", response_model=StocktakeResponse)
async def cancel_stocktake(
    session_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Abandon a count. An approved count can never be cancelled."""
    await _require_inventory_enabled(db, user.business_id)
    session = await _load_session(db, user.business_id, session_id)
    _reject_if_locked(session)
    session.status = StocktakeStatus.CANCELLED.value
    await db.commit()
    return await _build_response(db, user.business_id, await _load_session(db, user.business_id, session_id))


# ── Variance report ──────────────────────────────────────────────────────────

@router.get("/{session_id}/variance")
async def get_variance_report(
    session_id: uuid.UUID,
    flagged_only: bool = Query(False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    The management answer to three questions:

      "According to our sales, how much stock should we have?"  -> expected
      "How much do we actually have?"                           -> counted
      "Where is the difference coming from?"                    -> the terms

    Lines are ranked by absolute rand exposure, and the headline accuracy is
    value-weighted so a trivial miscount of a cheap ingredient cannot swamp a
    real loss of an expensive one.
    """
    await _require_inventory_enabled(db, user.business_id)
    session = await _load_session(db, user.business_id, session_id)
    response = await _build_response(db, user.business_id, session)

    lines = [line for line in response.lines if line.is_flagged] if flagged_only else response.lines

    return {
        "stocktake_id": str(session.id),
        "reference": session.reference,
        "status": session.status,
        "period_start_at": session.period_start_at.isoformat() if session.period_start_at else None,
        "cutoff_at": session.cutoff_at.isoformat() if session.cutoff_at else None,
        "accuracy_basis_points": response.accuracy_basis_points,
        "accuracy_percent": response.accuracy_percent,
        "alert_threshold_basis_points": DEFAULT_VARIANCE_ALERT_BASIS_POINTS,
        "total_variance_value_cents": response.total_variance_value_cents,
        "total_theoretical_value_cents": response.total_theoretical_value_cents,
        "formula": (
            "expected = opening + received - theoretical_consumption "
            "- wastage + adjustments;  variance = counted - expected;  "
            "variance % = variance / theoretical_consumption"
        ),
        "lines": [line.model_dump() for line in lines],
    }
