"""
External marketplace order routes — daily report import for Uber Eats, Mr D
and any generic delivery platform.

    POST /business/external-orders/preview   parse + resolve, write nothing
    POST /business/external-orders/commit    create orders + post stock
    GET  /business/external-orders/status    per-platform freshness
    GET  /business/external-orders/batches   import history
    GET/POST/DELETE .../mappings             saved product/modifier mappings
    GET  /business/external-orders/channels   sales by channel

OWNER/MANAGER ONLY. Importing a day of delivery sales changes stock and
reporting for the whole restaurant; it is not a cashier action.

Preview never writes. Commit is the only endpoint that creates anything, and
it refuses outright while any stock-affecting item is unmapped — accuracy
over convenience, because a silently skipped item understates usage forever
and the manager reads the gap as loss.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.analytics.revenue import (
    IS_SALE,
    gross_cents_expr,
    refunds_cents_expr,
    sale_count_expr,
    sale_timestamp_expr,
)
from backend.app.api.v1.routes_import import _read_rows, parse_mapping
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from backend.app.external_orders.adapters import adapt_rows
from backend.app.external_orders.service import (
    ResolutionSummary,
    import_resolved_orders,
    normalise_key,
    resolve_orders,
)
from shared.enums import (
    AuditScope,
    EXTERNAL_ORDER_SOURCES,
    ExternalImportStatus,
    ExternalMappingKind,
    ExternalPlatform,
    PLATFORM_TO_ORDER_SOURCE,
)
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.external_orders import ExternalImportBatch, ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order

router = APIRouter(prefix="/business/external-orders", tags=["external-orders"])

_PLATFORM_PATTERN = "^(UBER_EATS|MR_D|GENERIC)$"

PLATFORM_LABELS = {
    ExternalPlatform.UBER_EATS.value: "Uber Eats",
    ExternalPlatform.MR_D.value: "Mr D",
    ExternalPlatform.GENERIC.value: "Other platform",
}


async def _business(db: AsyncSession, business_id: uuid.UUID) -> Business:
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    return business


def _summary_payload(summary: ResolutionSummary) -> dict:
    """The shape the import screen renders — restaurant language, no internals."""
    return {
        "orders_total": len(summary.orders),
        "orders_ready": len(summary.importable),
        "duplicates": len(summary.duplicates),
        "cancelled": len(summary.cancelled),
        "blocked": len(summary.blocked),
        "unresolved_items": summary.unresolved_item_names,
        "unresolved_modifiers": summary.unresolved_modifier_names,
        "closed_period_cutoff": (
            summary.closed_period_cutoff.isoformat() if summary.closed_period_cutoff else None
        ),
        "orders_in_closed_period": len(summary.closed_period_orders),
        "orders": [
            {
                "external_order_id": o.external_order_id,
                "placed_at": o.placed_at.isoformat() if o.placed_at else None,
                "status": o.status,
                "gross_cents": o.gross_cents,
                "is_duplicate": o.is_duplicate,
                "is_cancelled": o.is_cancelled,
                "in_closed_period": o.in_closed_period,
                "blocks_import": o.blocks_import,
                "lines": [
                    {
                        "external_item_text": l.external_item_text,
                        "quantity": l.quantity,
                        "menu_item_name": l.menu_item_name,
                        "option_name": l.option_name,
                        "add_ons": len(l.add_on_ids),
                        "removals": len(l.removed_ref_ids),
                        "unresolved_modifiers": l.unresolved_modifiers,
                        "is_ignored": l.is_ignored,
                        "is_resolved": l.is_resolved,
                        "gross_cents": l.gross_cents,
                    }
                    for l in o.lines
                ],
            }
            for o in summary.orders[:200]
        ],
    }


# ── Preview ──────────────────────────────────────────────────────────────────

@router.post("/preview")
async def preview_external_orders(
    file: UploadFile = File(...),
    platform: str = Form(...),
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse and resolve a daily report. Writes nothing at all.

    Returns detected columns (so the manager can correct the mapping),
    per-order resolution, duplicates already imported, and anything still
    unmapped.
    """
    if platform not in PLATFORM_TO_ORDER_SOURCE:
        raise AppError("UNKNOWN_PLATFORM", f"Unknown platform {platform!r}", 422)
    business = await _business(db, user.business_id)

    rows = await _read_rows(file)
    adapted = adapt_rows(rows, platform=platform, mapping_override=parse_mapping(mapping))

    if adapted.missing_required:
        return {
            "platform": platform,
            "platform_label": PLATFORM_LABELS.get(platform, platform),
            "detected_columns": adapted.detected_columns,
            "resolved_mapping": adapted.resolved_mapping,
            "missing_required_columns": adapted.missing_required,
            "issues": [{"row": i.row, "message": i.message} for i in adapted.issues],
            "can_import": False,
            "message": (
                "We could not find these columns: "
                + ", ".join(adapted.missing_required)
                + ". Match them below and try again."
            ),
        }

    summary = await resolve_orders(
        db, business_id=user.business_id, platform=platform, orders=adapted.orders
    )
    payload = _summary_payload(summary)
    payload.update({
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "detected_columns": adapted.detected_columns,
        "resolved_mapping": adapted.resolved_mapping,
        "missing_required_columns": [],
        "issues": [{"row": i.row, "message": i.message} for i in adapted.issues],
        "rows_read": len(rows),
        "items_total": adapted.total_items,
        "inventory_enabled": business.inventory_enabled,
        "can_import": len(summary.blocked) == 0 and len(summary.importable) > 0,
    })
    return payload


# ── Commit ───────────────────────────────────────────────────────────────────

@router.post("/commit")
async def commit_external_orders(
    file: UploadFile = File(...),
    platform: str = Form(...),
    mapping: str | None = Form(default=None),
    include_cancelled: bool = Form(default=False),
    acknowledge_closed_period: bool = Form(default=False),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Import the file: create orders and post stock, in one transaction.

    Refuses while anything stock-affecting is unmapped. Orders already
    imported are skipped, not re-posted — the database index over
    (business, source, external order id) is the real guarantee, so even a
    simultaneous double upload cannot double-deduct.
    """
    if platform not in PLATFORM_TO_ORDER_SOURCE:
        raise AppError("UNKNOWN_PLATFORM", f"Unknown platform {platform!r}", 422)
    business = await _business(db, user.business_id)

    rows = await _read_rows(file)
    adapted = adapt_rows(rows, platform=platform, mapping_override=parse_mapping(mapping))
    if adapted.missing_required:
        raise AppError(
            "MISSING_COLUMNS",
            "Required columns are missing: " + ", ".join(adapted.missing_required),
            422,
        )

    summary = await resolve_orders(
        db, business_id=user.business_id, platform=platform, orders=adapted.orders
    )

    if summary.blocked:
        raise AppError(
            "UNRESOLVED_ITEMS",
            "Some items are not linked to your menu yet. Nothing was imported — "
            "map them first, otherwise your stock figures would be wrong.",
            422,
            details={
                "unresolved_items": summary.unresolved_item_names,
                "unresolved_modifiers": summary.unresolved_modifier_names,
                "blocked_orders": [o.external_order_id for o in summary.blocked],
            },
        )

    if summary.closed_period_orders and not acknowledge_closed_period:
        raise AppError(
            "CLOSED_STOCKTAKE_PERIOD",
            f"{len(summary.closed_period_orders)} order(s) were placed before your last "
            "approved stocktake. That count is signed off and will not be changed. "
            "Confirm to import them — their stock will be recorded just after the "
            "cut-off so the ingredients are not lost.",
            409,
            details={
                "cutoff_at": summary.closed_period_cutoff.isoformat()
                if summary.closed_period_cutoff else None,
                "orders": [o.external_order_id for o in summary.closed_period_orders],
            },
        )

    placed = [o.placed_at for o in summary.orders if o.placed_at]
    batch = ExternalImportBatch(
        business_id=user.business_id,
        platform=platform,
        status=ExternalImportStatus.PREVIEW.value,
        filename=(file.filename or "")[:255] or None,
        uploaded_by_user_id=user.user_id,
        uploaded_at=datetime.now(timezone.utc),
        period_start_at=min(placed) if placed else None,
        period_end_at=max(placed) if placed else None,
        total_rows=len(rows),
        accepted_rows=sum(len(o.lines) for o in summary.importable),
        duplicate_orders=len(summary.duplicates),
        rejected_rows=len(adapted.issues),
        issues_json=[{"row": i.row, "message": i.message} for i in adapted.issues][:200] or None,
    )
    db.add(batch)
    await db.flush()

    outcome = await import_resolved_orders(
        db,
        business=business,
        platform=platform,
        batch_id=batch.id,
        summary=summary,
        user_id=user.user_id,
        include_cancelled=include_cancelled,
        acknowledge_closed_period=acknowledge_closed_period,
    )

    batch.status = ExternalImportStatus.COMPLETED.value
    batch.orders_imported = outcome.orders_created
    batch.movements_created = outcome.movements_created
    batch.gross_sales_cents = outcome.gross_cents
    if outcome.backdated_into_closed_period:
        batch.notes = (
            f"{outcome.backdated_into_closed_period} order(s) fell inside a closed "
            "stocktake period; their stock was recorded just after the cut-off so "
            "the approved count was left unchanged."
        )

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=user.business_id,
        actor_user_id=user.user_id,
        action="external_orders.imported",
        target_type="external_import_batch",
        target_id=batch.id,
        diff_json={
            "platform": platform,
            "filename": batch.filename,
            "orders_imported": outcome.orders_created,
            "duplicates_skipped": outcome.duplicates_skipped,
            "movements_created": outcome.movements_created,
            "backdated_into_closed_period": outcome.backdated_into_closed_period,
        },
    ))

    await db.commit()

    return {
        "batch_id": str(batch.id),
        "platform": platform,
        "orders_imported": outcome.orders_created,
        "items_imported": outcome.items_created,
        "stock_movements_created": outcome.movements_created,
        "duplicates_skipped": outcome.duplicates_skipped,
        "cancelled_skipped": len(summary.cancelled) if not include_cancelled else 0,
        "gross_sales_cents": outcome.gross_cents,
        "backdated_into_closed_period": outcome.backdated_into_closed_period,
        "items_without_recipe": outcome.missing_recipe_items,
        "period_start_at": batch.period_start_at.isoformat() if batch.period_start_at else None,
        "period_end_at": batch.period_end_at.isoformat() if batch.period_end_at else None,
    }


# ── Freshness / status ───────────────────────────────────────────────────────

@router.get("/status")
async def external_sales_status(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Per-platform import freshness.

    This exists to stop a specific, expensive misreading: if today's delivery
    sales have not been imported, expected stock is overstated by exactly
    those sales, and a manager who does not know that reads the gap as loss.
    """
    now = datetime.now(timezone.utc)
    out = []

    for platform in (ExternalPlatform.UBER_EATS.value, ExternalPlatform.MR_D.value,
                     ExternalPlatform.GENERIC.value):
        row = (
            await db.execute(
                select(ExternalImportBatch)
                .where(
                    ExternalImportBatch.business_id == user.business_id,
                    ExternalImportBatch.platform == platform,
                    ExternalImportBatch.status == ExternalImportStatus.COMPLETED.value,
                )
                .order_by(ExternalImportBatch.uploaded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        source = PLATFORM_TO_ORDER_SOURCE[platform]
        # F7 fix (Wave 0A verifier): previously summed Order.total_cents over
        # EVERY row for this source — including CANCELLED/never-paid orders
        # that are not a "sale" anywhere else in the platform's reporting —
        # ignoring the single shared sale definition (analytics/revenue.py's
        # IS_SALE) every other revenue endpoint uses. Now uses the same
        # predicate so this figure is directly comparable to (and doesn't
        # contradict) the summary/channel-breakdown endpoints. This is GROSS
        # (pre-refund) — matches the field's existing name/label; it is NOT
        # netted against refund_amount_cents (see channel_breakdown's
        # separate net_cents if a netted figure is needed later).
        totals = (
            await db.execute(
                select(sale_count_expr(), gross_cents_expr()).where(
                    Order.business_id == user.business_id,
                    Order.source == source,
                )
            )
        ).one()

        covered_through = row.period_end_at if row else None
        hours_behind = (
            (now - covered_through).total_seconds() / 3600 if covered_through else None
        )
        # "Stale" once a full trading day is unaccounted for. Deliberately not
        # an alarm the moment the clock passes midnight.
        is_stale = covered_through is None or (hours_behind is not None and hours_behind > 24)

        out.append({
            "platform": platform,
            "label": PLATFORM_LABELS[platform],
            "ever_imported": row is not None,
            "last_import_at": row.uploaded_at.isoformat() if row else None,
            "covered_through": covered_through.isoformat() if covered_through else None,
            "last_orders_imported": row.orders_imported if row else 0,
            "hours_behind": round(hours_behind, 1) if hours_behind is not None else None,
            "is_stale": is_stale,
            "total_orders_all_time": int(totals[0] or 0),
            "total_gross_cents_all_time": int(totals[1] or 0),
        })

    any_stale = any(p["is_stale"] and p["ever_imported"] for p in out)
    never = [p["label"] for p in out if not p["ever_imported"]]

    return {
        "platforms": out,
        "any_stale": any_stale,
        "never_imported": never,
        "stock_warning": (
            "Expected stock may be overstated until today's delivery sales are imported."
            if any_stale else None
        ),
    }


@router.get("/batches")
async def list_batches(
    limit: int = Query(30, ge=1, le=200),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ExternalImportBatch)
            .where(ExternalImportBatch.business_id == user.business_id)
            .order_by(ExternalImportBatch.uploaded_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(b.id),
            "platform": b.platform,
            "platform_label": PLATFORM_LABELS.get(b.platform, b.platform),
            "status": b.status,
            "filename": b.filename,
            "uploaded_at": b.uploaded_at.isoformat() if b.uploaded_at else None,
            "period_start_at": b.period_start_at.isoformat() if b.period_start_at else None,
            "period_end_at": b.period_end_at.isoformat() if b.period_end_at else None,
            "total_rows": b.total_rows,
            "orders_imported": b.orders_imported,
            "duplicate_orders": b.duplicate_orders,
            "rejected_rows": b.rejected_rows,
            "movements_created": b.movements_created,
            "gross_sales_cents": b.gross_sales_cents,
            "notes": b.notes,
        }
        for b in rows
    ]


# ── Mappings ─────────────────────────────────────────────────────────────────

class MappingCreateRequest(BaseModel):
    platform: str = Field(pattern=_PLATFORM_PATTERN)
    kind: str = Field(default="ITEM", pattern="^(ITEM|MODIFIER)$")
    external_text: str = Field(min_length=1, max_length=500)
    menu_item_id: uuid.UUID | None = None
    option_id: str | None = Field(default=None, max_length=64)
    menu_add_on_id: uuid.UUID | None = None
    removable_ref_id: str | None = Field(default=None, max_length=64)
    is_ignored: bool = False


@router.get("/mappings")
async def list_mappings(
    platform: str | None = Query(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ExternalProductMapping).where(
        ExternalProductMapping.business_id == user.business_id
    )
    if platform:
        stmt = stmt.where(ExternalProductMapping.platform == platform)
    rows = (await db.execute(stmt.order_by(ExternalProductMapping.external_text))).scalars().all()

    item_names = {
        m.id: m.name
        for m in (
            await db.execute(
                select(MenuItem).where(MenuItem.business_id == user.business_id)
            )
        ).scalars().all()
    }
    add_on_names = {
        a.id: a.name
        for a in (
            await db.execute(
                select(MenuAddOn).where(MenuAddOn.business_id == user.business_id)
            )
        ).scalars().all()
    }

    return [
        {
            "id": str(m.id),
            "platform": m.platform,
            "kind": m.kind,
            "external_text": m.external_text,
            "menu_item_id": str(m.menu_item_id) if m.menu_item_id else None,
            "menu_item_name": item_names.get(m.menu_item_id),
            "option_id": m.option_id,
            "menu_add_on_id": str(m.menu_add_on_id) if m.menu_add_on_id else None,
            "menu_add_on_name": add_on_names.get(m.menu_add_on_id),
            "removable_ref_id": m.removable_ref_id,
            "is_ignored": m.is_ignored,
            "times_used": m.times_used,
            "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
        }
        for m in rows
    ]


@router.post("/mappings", status_code=201)
async def upsert_mapping(
    body: MappingCreateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Remember how an external name maps onto this menu, so tomorrow's file
    maps itself.

    A mapping never creates a product — it only points at something already
    on this restaurant's menu, which is what keeps ONE canonical recipe
    across every channel.
    """
    if not body.is_ignored:
        if body.kind == ExternalMappingKind.ITEM.value and not body.menu_item_id:
            raise AppError(
                "MAPPING_TARGET_REQUIRED",
                "Choose the menu item this product is, or mark it as not affecting stock",
                422,
            )
        if body.kind == ExternalMappingKind.MODIFIER.value and not (
            body.menu_add_on_id or body.removable_ref_id or body.option_id
        ):
            raise AppError(
                "MAPPING_TARGET_REQUIRED",
                "Choose what this modifier means, or mark it as not affecting stock",
                422,
            )

    # Targets must belong to THIS tenant — never trust an id alone.
    if body.menu_item_id:
        exists = (
            await db.execute(
                select(MenuItem.id).where(
                    MenuItem.id == body.menu_item_id,
                    MenuItem.business_id == user.business_id,
                )
            )
        ).first()
        if not exists:
            raise NotFoundError("Menu item", str(body.menu_item_id))
    if body.menu_add_on_id:
        exists = (
            await db.execute(
                select(MenuAddOn.id).where(
                    MenuAddOn.id == body.menu_add_on_id,
                    MenuAddOn.business_id == user.business_id,
                )
            )
        ).first()
        if not exists:
            raise NotFoundError("Add-on", str(body.menu_add_on_id))

    key = normalise_key(body.external_text)
    existing = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.business_id == user.business_id,
                ExternalProductMapping.platform == body.platform,
                ExternalProductMapping.kind == body.kind,
                ExternalProductMapping.external_key == key,
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.external_text = body.external_text
        existing.menu_item_id = body.menu_item_id
        existing.option_id = body.option_id
        existing.menu_add_on_id = body.menu_add_on_id
        existing.removable_ref_id = body.removable_ref_id
        existing.is_ignored = body.is_ignored
        mapping = existing
    else:
        mapping = ExternalProductMapping(
            business_id=user.business_id,
            platform=body.platform,
            kind=body.kind,
            external_text=body.external_text,
            external_key=key,
            menu_item_id=body.menu_item_id,
            option_id=body.option_id,
            menu_add_on_id=body.menu_add_on_id,
            removable_ref_id=body.removable_ref_id,
            is_ignored=body.is_ignored,
            created_by_user_id=user.user_id,
        )
        db.add(mapping)

    await db.commit()
    await db.refresh(mapping)
    return {"id": str(mapping.id), "external_text": mapping.external_text}


@router.delete("/mappings/{mapping_id}", status_code=204)
async def delete_mapping(
    mapping_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    mapping = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.id == mapping_id,
                ExternalProductMapping.business_id == user.business_id,
            )
        )
    ).scalar_one_or_none()
    if not mapping:
        raise NotFoundError("Mapping", str(mapping_id))
    await db.delete(mapping)
    await db.commit()
    return None


# ── Channel reporting ────────────────────────────────────────────────────────

@router.get("/channels")
async def channel_breakdown(
    days: int = Query(7, ge=1, le=90),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Sales by channel. Deliberately simple for the pilot — commission and
    settlement are not modelled, but the data needed to add them later
    (per-order gross, per-channel source) is all recorded.

    BUG-0043 fix: now uses the shared sale definition
    (backend/app/analytics/revenue.py) instead of a narrower, ad hoc
    `payment_status IN (PAID, PARTIALLY_REFUNDED)` filter, which silently
    excluded a fully REFUNDED order (still a real sale, just netting to 0)
    and any WhatsApp cash-on-collection order (payment_status never becomes
    PAID for those — only the fulfilment status does). The date cutoff also
    now uses the sale's own timestamp (external_placed_at for imports,
    created_at otherwise) rather than row-insert time.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                Order.source,
                func.count(),
                func.coalesce(func.sum(Order.total_cents), 0),
                func.coalesce(func.sum(Order.tax_cents), 0),
                refunds_cents_expr(),
            )
            .where(
                Order.business_id == user.business_id,
                sale_timestamp_expr() >= since,
                IS_SALE,
            )
            .group_by(Order.source)
        )
    ).all()

    channels = [
        {
            "source": source,
            "label": {
                "POS": "In store (till)", "WHATSAPP": "WhatsApp", "MANUAL": "Manual",
                "ADMIN": "Admin", "UBER_EATS": "Uber Eats", "MR_D": "Mr D",
                "OTHER_EXTERNAL": "Other platform",
            }.get(source, source),
            "is_external": source in EXTERNAL_ORDER_SOURCES,
            "order_count": int(count or 0),
            "gross_cents": int(gross or 0),
            "vat_cents": int(vat or 0),
            "refunds_cents": int(refunds or 0),
            "net_cents": int(gross or 0) - int(refunds or 0),
        }
        for source, count, gross, vat, refunds in rows
    ]
    channels.sort(key=lambda c: -c["gross_cents"])

    return {
        "days": days,
        "since": since.isoformat(),
        "channels": channels,
        "total_orders": sum(c["order_count"] for c in channels),
        "total_gross_cents": sum(c["gross_cents"] for c in channels),
        "total_refunds_cents": sum(c["refunds_cents"] for c in channels),
        "total_net_cents": sum(c["net_cents"] for c in channels),
        "external_gross_cents": sum(
            c["gross_cents"] for c in channels if c["is_external"]
        ),
    }
