"""
External order import: resolving canonical external orders against this
tenant's menu, and posting them through the normal sales pipeline.

WHAT THIS DOES AND DOES NOT DECIDE
----------------------------------
It decides *what a row means* for this restaurant. It does not re-implement
inventory: an imported order becomes a real ``Order`` and the existing
``post_sale_consumption`` resolves the same recipes and writes to the same
ledger as a till sale.

FOUR RULES THAT PROTECT ACCURACY
--------------------------------
1. **Never guess a product.** An unmapped item blocks the import rather than
   being skipped, because a silently skipped item understates usage forever
   and the manager reads the gap as loss.
2. **Never import twice.** Duplicate protection is a database index over
   (business, source, external order id), not a UI check.
3. **Stock is dated when the sale happened**, not when the file was
   uploaded — otherwise a morning import moves yesterday's consumption into
   today and both days are wrong.
4. **A financial cancellation is not a production fact.** A cancelled order
   in a report says nothing about whether the kitchen already made the food,
   so it is never auto-consumed and never auto-excluded; a human decides.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.external_orders.adapters import (
    CanonicalExternalOrder,
    looks_cancelled,
    order_id_prefix,
)
from backend.app.inventory.ledger import post_sale_consumption
from shared.enums import (
    ExternalMappingKind,
    ExternalPlatform,
    OrderStatus,
    PLATFORM_TO_ORDER_SOURCE,
)
from shared.models.delivery import DeliveryOrder
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.stocktake import StocktakeSession
from shared.pricing.engine import calculate_vat_cents

#: How an externally-paid order records its payment. The restaurant never
#: touched this money, so it must never reach till cash-up — which it cannot,
#: because these orders carry no till_session_id.
EXTERNAL_PAYMENT_METHOD = "EXTERNAL_PLATFORM"

#: A provider's short display code (Uber "BC953") is reused over time, so a
#: report row carrying one only counts as the same sale as a live-API order
#: placed within this window. Shared with backend/app/delivery/order_service.py,
#: which applies the same rule in the other direction.
LIVE_DISPLAY_ID_MATCH_WINDOW = timedelta(days=1)

#: Which live-API provider (delivery_orders.provider) a report platform is.
#: Mr D is deliberately absent: it has no live connection (no partner API —
#: docs/MRD_API_ACCESS_2026-10.md), so a Mr D report can never collide with
#: one. Add it here in the same change that ever enables a live Mr D path.
_PLATFORM_TO_LIVE_PROVIDER = {
    ExternalPlatform.UBER_EATS.value: "UBER_EATS",
}


def normalise_key(text: str) -> str:
    """Lookup form for a mapping: 'Large  Margherita ' -> 'large margherita'."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


# ── Resolution results ───────────────────────────────────────────────────────


@dataclass
class ResolvedLine:
    external_item_text: str
    quantity: int
    menu_item_id: uuid.UUID | None = None
    menu_item_name: str | None = None
    option_id: str | None = None
    option_name: str | None = None
    add_on_ids: list[uuid.UUID] = field(default_factory=list)
    removed_ref_ids: list[str] = field(default_factory=list)
    unresolved_modifiers: list[str] = field(default_factory=list)
    gross_cents: int | None = None
    is_ignored: bool = False

    @property
    def is_resolved(self) -> bool:
        return self.is_ignored or self.menu_item_id is not None


@dataclass
class ResolvedOrder:
    external_order_id: str
    placed_at: datetime
    status: str | None
    gross_cents: int | None
    lines: list[ResolvedLine]
    is_duplicate: bool = False
    is_cancelled: bool = False
    in_closed_period: bool = False
    #: Why it is a duplicate: "IMPORTED" (an earlier report import) or
    #: "LIVE_API" (the live provider connection already booked this sale).
    duplicate_source: str | None = None

    @property
    def unresolved_items(self) -> list[str]:
        return [l.external_item_text for l in self.lines if not l.is_resolved]

    @property
    def unresolved_modifiers(self) -> list[str]:
        return [m for l in self.lines for m in l.unresolved_modifiers]

    @property
    def blocks_import(self) -> bool:
        """
        Would importing this order silently understate stock? An unmapped
        product or modifier means yes.
        """
        return bool(self.unresolved_items or self.unresolved_modifiers)


@dataclass
class ResolutionSummary:
    orders: list[ResolvedOrder] = field(default_factory=list)
    closed_period_cutoff: datetime | None = None

    @property
    def importable(self) -> list[ResolvedOrder]:
        return [
            o for o in self.orders
            if not o.is_duplicate and not o.is_cancelled and not o.blocks_import
        ]

    @property
    def duplicates(self) -> list[ResolvedOrder]:
        return [o for o in self.orders if o.is_duplicate]

    @property
    def cancelled(self) -> list[ResolvedOrder]:
        return [o for o in self.orders if o.is_cancelled and not o.is_duplicate]

    @property
    def blocked(self) -> list[ResolvedOrder]:
        return [o for o in self.orders if not o.is_duplicate and o.blocks_import]

    @property
    def closed_period_orders(self) -> list[ResolvedOrder]:
        return [o for o in self.importable if o.in_closed_period]

    @property
    def unresolved_item_names(self) -> list[str]:
        seen: list[str] = []
        for order in self.orders:
            for name in order.unresolved_items:
                if name not in seen:
                    seen.append(name)
        return seen

    @property
    def unresolved_modifier_names(self) -> list[str]:
        seen: list[str] = []
        for order in self.orders:
            for name in order.unresolved_modifiers:
                if name not in seen:
                    seen.append(name)
        return seen


# ── Resolution ───────────────────────────────────────────────────────────────


async def load_mappings(
    db: AsyncSession, business_id: uuid.UUID, platform: str
) -> dict[tuple[str, str], ExternalProductMapping]:
    """Saved mappings for this tenant and platform, keyed by (kind, key)."""
    rows = (
        await db.execute(
            select(ExternalProductMapping).where(
                ExternalProductMapping.business_id == business_id,
                ExternalProductMapping.platform == platform,
            )
        )
    ).scalars().all()
    return {(m.kind, m.external_key): m for m in rows}


async def _last_approved_cutoff(db: AsyncSession, business_id: uuid.UUID) -> datetime | None:
    session = (
        await db.execute(
            select(StocktakeSession)
            .where(
                StocktakeSession.business_id == business_id,
                StocktakeSession.status == "APPROVED",
            )
            .order_by(StocktakeSession.cutoff_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return session.cutoff_at if session else None


def _match_option(menu_item: MenuItem, text: str) -> tuple[str | None, str | None]:
    """
    Find a size/option on the item whose name appears in the external text.

    Longest name first, so "Extra Large" is not matched as "Large".
    """
    key = normalise_key(text)
    candidates: list[tuple[str, str]] = []
    for group in ((menu_item.options_json or {}).get("option_groups") or []):
        for option in (group.get("options") or []):
            if option.get("id") and option.get("name"):
                candidates.append((str(option["id"]), str(option["name"])))
    for option_id, name in sorted(candidates, key=lambda c: -len(c[1])):
        if normalise_key(name) and normalise_key(name) in key:
            return option_id, name
    return None, None


async def resolve_orders(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    platform: str,
    orders: list[CanonicalExternalOrder],
) -> ResolutionSummary:
    """
    Work out what each external line means here, without writing anything.

    Auto-matching is deliberately conservative: an exact saved mapping, or an
    exact menu-item name match. Anything fuzzier is surfaced for the manager
    rather than guessed, because a wrong product mapping silently deducts the
    wrong ingredients every day until somebody notices.
    """
    summary = ResolutionSummary()
    source = PLATFORM_TO_ORDER_SOURCE.get(platform)
    mappings = await load_mappings(db, business_id, platform)
    cutoff = await _last_approved_cutoff(db, business_id)
    summary.closed_period_cutoff = cutoff

    menu_items = (
        await db.execute(
            select(MenuItem).where(
                MenuItem.business_id == business_id,
                MenuItem.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    by_name = {normalise_key(m.name): m for m in menu_items}
    by_id = {m.id: m for m in menu_items}

    add_ons = (
        await db.execute(
            select(MenuAddOn).where(
                MenuAddOn.business_id == business_id,
                MenuAddOn.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    add_on_by_name = {normalise_key(a.name): a for a in add_ons}
    add_on_by_id = {a.id: a for a in add_ons}

    existing_ids = set()
    incoming = [o.external_order_id for o in orders]
    if incoming and source:
        rows = (
            await db.execute(
                select(Order.external_order_id).where(
                    Order.business_id == business_id,
                    Order.source == source,
                    Order.external_order_id.in_(incoming),
                )
            )
        ).all()
        existing_ids = {r[0] for r in rows}

    live_orders = await _live_api_orders(db, business_id=business_id, platform=platform, incoming=incoming)

    for order in orders:
        resolved_lines: list[ResolvedLine] = []

        for line in order.lines:
            resolved = ResolvedLine(
                external_item_text=line.external_item_text,
                quantity=line.quantity,
                gross_cents=line.gross_cents,
            )
            item_key = normalise_key(line.external_item_text)
            mapping = mappings.get((ExternalMappingKind.ITEM.value, item_key))

            if mapping and mapping.is_ignored:
                resolved.is_ignored = True
            elif mapping and mapping.menu_item_id in by_id:
                menu_item = by_id[mapping.menu_item_id]
                resolved.menu_item_id = menu_item.id
                resolved.menu_item_name = menu_item.name
                if mapping.option_id:
                    resolved.option_id = mapping.option_id
                    _, resolved.option_name = _match_option(menu_item, mapping.option_id)
                    if not resolved.option_name:
                        for group in ((menu_item.options_json or {}).get("option_groups") or []):
                            for option in (group.get("options") or []):
                                if str(option.get("id")) == mapping.option_id:
                                    resolved.option_name = option.get("name")
            elif item_key in by_name:
                # Exact name match — the item is called the same thing on both
                # sides, which needs no human decision.
                menu_item = by_name[item_key]
                resolved.menu_item_id = menu_item.id
                resolved.menu_item_name = menu_item.name

            # Size text may live in its own column even when the item matched.
            if resolved.menu_item_id and not resolved.option_id:
                menu_item = by_id[resolved.menu_item_id]
                probe = line.size_text or line.external_item_text
                option_id, option_name = _match_option(menu_item, probe)
                if option_id:
                    resolved.option_id = option_id
                    resolved.option_name = option_name

            # Modifiers. Anything that cannot be resolved is reported, never
            # dropped — an ignored "Extra Cheese" is 50 g of cheese a day that
            # quietly turns into apparent shrinkage.
            for modifier in line.modifiers:
                mod_key = normalise_key(modifier)
                mod_mapping = mappings.get((ExternalMappingKind.MODIFIER.value, mod_key))

                if mod_mapping and mod_mapping.is_ignored:
                    continue
                if mod_mapping and mod_mapping.menu_add_on_id in add_on_by_id:
                    resolved.add_on_ids.append(mod_mapping.menu_add_on_id)
                    continue
                if mod_mapping and mod_mapping.removable_ref_id:
                    resolved.removed_ref_ids.append(mod_mapping.removable_ref_id)
                    continue
                if mod_mapping and mod_mapping.option_id:
                    resolved.option_id = mod_mapping.option_id
                    continue

                if mod_key in add_on_by_name:
                    resolved.add_on_ids.append(add_on_by_name[mod_key].id)
                    continue

                # "No mushroom" -> a removable ingredient on the matched item.
                stripped = re.sub(r"^(no|without|remove|hold(?: the)?)\s+", "", mod_key).strip()
                if stripped != mod_key and resolved.menu_item_id:
                    menu_item = by_id[resolved.menu_item_id]
                    removables = (menu_item.options_json or {}).get("removable_ingredients") or []
                    match = next(
                        (r for r in removables if normalise_key(r.get("name", "")) == stripped),
                        None,
                    )
                    if match and match.get("id"):
                        resolved.removed_ref_ids.append(str(match["id"]))
                        continue

                resolved.unresolved_modifiers.append(modifier)

            resolved_lines.append(resolved)

        placed = order.placed_at
        duplicate_source = None
        if order.external_order_id in existing_ids:
            duplicate_source = "IMPORTED"
        elif _matches_live_order(order.external_order_id, placed, live_orders):
            duplicate_source = "LIVE_API"
        summary.orders.append(
            ResolvedOrder(
                external_order_id=order.external_order_id,
                placed_at=placed,
                status=order.status,
                gross_cents=order.gross_cents,
                lines=resolved_lines,
                is_duplicate=duplicate_source is not None,
                is_cancelled=looks_cancelled(order.status),
                in_closed_period=bool(cutoff and placed and placed <= cutoff),
                duplicate_source=duplicate_source,
            )
        )

    return summary


async def _live_api_orders(
    db: AsyncSession, *, business_id: uuid.UUID, platform: str, incoming: list[str],
) -> list[DeliveryOrder]:
    """
    Live-API delivery orders that already booked a sale and whose provider
    id OR short display code appears in this file. A provider report may
    carry either; without this check, a restaurant using both the live
    connection and a report import would get two Orders (and two stock
    deductions) for one real sale. Tenant-scoped like every other read here.
    """
    provider = _PLATFORM_TO_LIVE_PROVIDER.get(platform)
    if not provider or not incoming:
        return []
    return list((
        await db.execute(
            select(DeliveryOrder).where(
                DeliveryOrder.business_id == business_id,
                DeliveryOrder.provider == provider,
                DeliveryOrder.order_id.isnot(None),
                (DeliveryOrder.external_order_id.in_(incoming)) | (DeliveryOrder.display_id.in_(incoming)),
            )
        )
    ).scalars().all())


def _matches_live_order(
    external_order_id: str, placed_at: datetime | None, live_orders: list[DeliveryOrder],
) -> bool:
    for live in live_orders:
        if live.external_order_id == external_order_id:
            return True
        if live.display_id and live.display_id == external_order_id:
            anchor = live.placed_at or live.created_at
            if placed_at is None or anchor is None:
                continue  # a reused short code with no time to compare is not proof
            if abs(placed_at - anchor) <= LIVE_DISPLAY_ID_MATCH_WINDOW:
                return True
    return False


# ── Posting ──────────────────────────────────────────────────────────────────


@dataclass
class ImportOutcome:
    orders_created: int = 0
    items_created: int = 0
    movements_created: int = 0
    duplicates_skipped: int = 0
    gross_cents: int = 0
    backdated_into_closed_period: int = 0
    missing_recipe_items: list[str] = field(default_factory=list)


async def import_resolved_orders(
    db: AsyncSession,
    *,
    business,
    platform: str,
    batch_id: uuid.UUID,
    summary: ResolutionSummary,
    user_id: uuid.UUID | None,
    include_cancelled: bool = False,
    acknowledge_closed_period: bool = False,
) -> ImportOutcome:
    """
    Create orders and post stock for everything importable.

    Does NOT commit — the caller owns the transaction, so a file either
    imports completely or not at all.

    CLOSED-PERIOD POLICY
    --------------------
    An order placed before the last APPROVED stocktake's cut-off cannot be
    dated into that period: the signed-off variance report is immutable, and
    the live view only counts movements after the cut-off, so a movement
    dated earlier would vanish from both — the consumption would simply
    disappear.

    So when the manager acknowledges the conflict, the ORDER keeps its true
    ``external_placed_at`` (history stays honest) while its stock movement is
    dated one second AFTER the cut-off, with a note saying so. The approved
    report is never rewritten, and the ingredients are never lost.
    """
    outcome = ImportOutcome()
    source = PLATFORM_TO_ORDER_SOURCE[platform]
    now = datetime.now(timezone.utc)
    cutoff = summary.closed_period_cutoff

    candidates = list(summary.importable)
    if include_cancelled:
        candidates += [o for o in summary.cancelled if not o.blocks_import]

    outcome.duplicates_skipped = len(summary.duplicates)

    for resolved in candidates:
        if resolved.in_closed_period and not acknowledge_closed_period:
            continue

        # Stock time: the sale's own moment, unless that lands inside a
        # locked period — see the docstring.
        movement_at = resolved.placed_at
        note = None
        if cutoff and resolved.placed_at and resolved.placed_at <= cutoff:
            movement_at = cutoff + timedelta(seconds=1)
            note = (
                f"Ordered {resolved.placed_at.isoformat()}, inside the stocktake "
                f"period closed at {cutoff.isoformat()}. Stock was recorded "
                f"immediately after the cut-off so the approved count stays "
                f"unchanged and the ingredients are not lost."
            )
            outcome.backdated_into_closed_period += 1

        gross = resolved.gross_cents or 0
        # VAT is derived from INSIDE the platform's consumer price. A R140
        # Uber sale is R140 to the customer; adding 15% would invent revenue.
        vat_cents = (
            calculate_vat_cents(gross, business.vat_rate_basis_points, True)
            if getattr(business, "vat_registered", False) else 0
        )

        order = Order(
            business_id=business.id,
            order_number=f"{order_id_prefix(platform)}-{resolved.external_order_id}"[:32],
            status=OrderStatus.COMPLETED.value,
            # The platform already took the customer's money. Recording it as
            # PAID with no till session is what keeps it out of cash-up.
            payment_status="PAID",
            payment_required=False,
            payment_method=EXTERNAL_PAYMENT_METHOD,
            paid_at=resolved.placed_at,
            till_session_id=None,
            cash_tendered_cents=None,
            change_due_cents=None,
            source=source,
            order_mode="DELIVERY",
            subtotal_cents=gross,
            delivery_fee_cents=0,
            discount_cents=0,
            tax_cents=vat_cents,
            total_cents=gross,
            currency=getattr(business, "currency", "ZAR") or "ZAR",
            external_order_id=resolved.external_order_id,
            external_batch_id=batch_id,
            external_placed_at=resolved.placed_at,
            external_imported_at=now,
            external_status=resolved.status,
            created_by_user_id=user_id,
            confirmed_at=resolved.placed_at,
            completed_at=resolved.placed_at,
        )
        db.add(order)
        await db.flush()

        resolved_items = []
        for line in resolved.lines:
            if line.is_ignored or not line.menu_item_id:
                continue

            options_snapshot = []
            if line.option_id:
                options_snapshot.append({
                    "group_id": "size",
                    "group_name": "Size",
                    "option_id": line.option_id,
                    "option_name": line.option_name or line.option_id,
                    "price_delta_cents": 0,
                })
            add_ons_snapshot = [
                {"add_on_id": str(a), "name": "", "quantity": 1, "price_cents": 0}
                for a in line.add_on_ids
            ]
            removed_snapshot = [{"id": r, "name": ""} for r in line.removed_ref_ids]

            db.add(OrderItem(
                order_id=order.id,
                business_id=business.id,
                menu_item_id=line.menu_item_id,
                name_snapshot=line.menu_item_name or line.external_item_text,
                base_price_cents=0,
                option_adjustment_cents=0,
                add_on_total_cents=0,
                unit_price_cents=(
                    int((line.gross_cents or 0) / line.quantity) if line.quantity else 0
                ),
                quantity=line.quantity,
                line_total_cents=line.gross_cents or 0,
                selected_options_snapshot=options_snapshot or None,
                add_ons_snapshot=add_ons_snapshot or None,
                removed_ingredients_snapshot=removed_snapshot or None,
                special_instructions=f"{platform}: {line.external_item_text}",
            ))
            outcome.items_created += 1

            # Shape the pricing layer's contract so the SAME consumption
            # resolver serves imports and till sales — one canonical recipe.
            import types as _types

            resolved_items.append(_types.SimpleNamespace(
                menu_item_id=line.menu_item_id,
                breakdown=_types.SimpleNamespace(quantity=line.quantity),
                selected_options_snapshot=options_snapshot,
                add_ons_snapshot=add_ons_snapshot,
                removed_ingredients_snapshot=removed_snapshot,
            ))

        db.add(OrderEvent(
            order_id=order.id,
            business_id=business.id,
            old_status=None,
            new_status=OrderStatus.COMPLETED.value,
            changed_by_user_id=user_id,
            reason=note or f"Imported from {platform} order {resolved.external_order_id}",
        ))

        if resolved_items and getattr(business, "inventory_enabled", False):
            consumption = await post_sale_consumption(
                db,
                business_id=business.id,
                order_id=order.id,
                resolved_items=resolved_items,
                created_by_user_id=user_id,
                occurred_at=movement_at,
            )
            outcome.movements_created += len(consumption.lines)
            for missing in consumption.menu_items_without_recipe:
                name = next(
                    (l.menu_item_name for l in resolved.lines if l.menu_item_id == missing),
                    str(missing),
                )
                if name and name not in outcome.missing_recipe_items:
                    outcome.missing_recipe_items.append(name)

        outcome.orders_created += 1
        outcome.gross_cents += gross

    return outcome
