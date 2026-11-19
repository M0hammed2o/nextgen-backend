"""
Temporary events (souk): event setup, bulk vendor onboarding and event
subscriptions. SUPER_ADMIN only. Events are platform-level data, the same
plane as business management.

Every vendor is an ordinary Business (DECISION-0062 option A), created
through admin_api/app/services/provisioning.py with:
- plan = EVENT_PLAN, so only the event access job ever switches it on or off
  (backend/app/events/service.py);
- industry EVENT_FOOD / EVENT_RETAIL, with POS and inventory enabled;
- no external identifier of any kind. WhatsApp numbers, delivery stores and
  payment credentials are never copied or set here.

Pricing comes only from backend/app/events/rules.py: owner decisions
R100/day food and R50/day clothing, VAT-inclusive, per billable event day.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from typing import Annotated
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from admin_api.app.core.deps import require_super_admin
from admin_api.app.services.provisioning import ensure_owner_email_free, provision_business
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from backend.app.events import registration as reg
from backend.app.events.rules import DEFAULT_PREP_DAYS, PaidRange, evaluate_access, quote
from backend.app.events.subscriptions import (
    assert_no_overlap,
    is_payment_reference_violation,
    normalise_payment_reference,
    payment_reference_in_use,
)
from backend.app.events.service import (
    EVENT_ADMIN_SUSPENSION_REASON,
    EVENT_SUSPENSION_REASON,
    reconcile_event_vendor_access,
)
from shared.enums import EVENT_PLAN, AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.event import Event, EventDay, EventVendor, EventVendorSubscription
from shared.utils import generate_temp_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/events", tags=["admin-events"])

# Each new vendor costs one bcrypt hash (~1 s of CPU). Hashes are computed concurrently in the
# threadpool (see bulk_onboard_vendors); 100 keeps one request under gateway timeouts.
# Larger lists are imported in several batches.
MAX_BULK_VENDORS = 100
_CATEGORY_INDUSTRY = {"FOOD": "EVENT_FOOD", "CLOTHING": "EVENT_RETAIL"}


# ── Schemas ───────────────────────────────────────────────────────────────────

class EventDayIn(BaseModel):
    day: date
    is_billable: bool = True


class EventCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    venue: str | None = Field(default=None, max_length=255)
    timezone: str = "Africa/Johannesburg"
    # Capped at R10,000/day: keeps rate x days far inside the int32 amount column.
    food_day_rate_cents: int = Field(default=10_000, ge=0, le=1_000_000)
    clothing_day_rate_cents: int = Field(default=5_000, ge=0, le=1_000_000)
    days: list[EventDayIn] = Field(default_factory=list, max_length=366)
    notes: str | None = None
    prep_days: int = Field(default=DEFAULT_PREP_DAYS, ge=0, le=90)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone")
        return v


class EventUpdate(BaseModel):
    """Editable event fields. Rates are deliberately not editable: they are frozen into quotes."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=255)
    venue: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    prep_days: int | None = Field(default=None, ge=0, le=90)


class VendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stall_code: str | None = Field(default=None, max_length=64)


class VendorSuspend(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EventStatusUpdate(BaseModel):
    status: str = Field(pattern="^(DRAFT|OPEN|CLOSED|CANCELLED)$")


class CalendarUpdate(BaseModel):
    days: list[EventDayIn] = Field(max_length=366)


class VendorRow(BaseModel):
    category: str = Field(pattern="^(FOOD|CLOTHING)$")
    stall_code: str | None = Field(default=None, max_length=64)
    # Either an existing event-plan business (a returning vendor) ...
    existing_business_id: uuid.UUID | None = None
    # ... or a new one.
    business_name: str | None = Field(default=None, min_length=2, max_length=255)
    owner_email: EmailStr | None = None
    owner_full_name: str | None = Field(default=None, max_length=255)
    # Optional: quote an UNPAID subscription straight away.
    starts_on: date | None = None
    ends_on: date | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.existing_business_id is None and not (self.business_name and self.owner_email):
            raise ValueError("a new vendor needs business_name and owner_email")
        if (self.starts_on is None) != (self.ends_on is None):
            raise ValueError("give both starts_on and ends_on, or neither")
        return self


class BulkVendorRequest(BaseModel):
    vendors: list[VendorRow] = Field(min_length=1, max_length=MAX_BULK_VENDORS)
    dry_run: bool = False


class BulkVendorCsvRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=2_000_000)
    dry_run: bool = False


class RegistrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open: bool
    rotate_code: bool = False


class SubscriptionCreate(BaseModel):
    starts_on: date
    ends_on: date


class MarkPaid(BaseModel):
    payment_reference: str = Field(min_length=1, max_length=255)

    @field_validator("payment_reference")
    @classmethod
    def _normalise(cls, v: str) -> str:
        v = normalise_payment_reference(v)
        if not v:
            raise ValueError("payment_reference must not be blank")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _load_event(db: AsyncSession, event_id: uuid.UUID, lock: bool = False) -> Event:
    """
    With ``lock=True`` the event row is locked (SELECT ... FOR UPDATE) until the
    transaction ends, so mark-paid, create-subscription and calendar-replace
    for one event are serialized. Days are read after the lock is held.
    """
    stmt = select(Event).where(Event.id == event_id).options(selectinload(Event.days))
    if lock:
        stmt = stmt.with_for_update(of=Event)
    event = (await db.execute(stmt)).scalar_one_or_none()
    if not event:
        raise NotFoundError("Event", str(event_id))
    return event


def _billable(event: Event) -> list[date]:
    return [d.day for d in event.days if d.is_billable]


def _quote_or_422(event: Event, category: str, starts_on: date, ends_on: date):
    try:
        return quote(category, event.food_day_rate_cents, event.clothing_day_rate_cents,
                     _billable(event), starts_on, ends_on)
    except ValueError as exc:
        raise AppError("INVALID_SUBSCRIPTION_RANGE", str(exc), 422)


async def _unique_event_slug(db: AsyncSession, name: str) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "event"
    for suffix in range(50):
        candidate = base if suffix == 0 else f"{base}-{suffix}"
        if not (await db.execute(select(Event.id).where(Event.slug == candidate))).scalar_one_or_none():
            return candidate
    raise AppError("SLUG_GENERATION_FAILED", "Could not generate unique event slug", 500)


def _audit(user: AuthUser, action: str, target_type: str, target_id: uuid.UUID, diff: dict,
           business_id: uuid.UUID | None = None) -> AuditEvent:
    return AuditEvent(scope=AuditScope.PLATFORM.value, business_id=business_id, actor_user_id=user.user_id,
                      action=action, target_type=target_type, target_id=target_id, diff_json=diff)


def _dupes(values: list[str]) -> set[str]:
    seen, dupes = set(), set()
    for v in values:
        (dupes if v in seen else seen).add(v)
    return dupes


# ── Events ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_event(body: EventCreate, user: AuthUser = Depends(require_super_admin),
                       db: AsyncSession = Depends(get_db)):
    days = {d.day: d.is_billable for d in body.days}
    if len(days) != len(body.days):
        raise AppError("DUPLICATE_EVENT_DAY", "each date may appear only once", 422)
    event = Event(name=body.name, slug=await _unique_event_slug(db, body.name), venue=body.venue,
                  timezone=body.timezone, status="DRAFT", food_day_rate_cents=body.food_day_rate_cents,
                  clothing_day_rate_cents=body.clothing_day_rate_cents, notes=body.notes,
                  prep_days=body.prep_days)
    db.add(event)
    await db.flush()
    db.add_all([EventDay(event_id=event.id, day=d, is_billable=b) for d, b in days.items()])
    db.add(_audit(user, "admin.event.created", "event", event.id,
                  {"name": event.name, "days": len(days), "food_day_rate_cents": event.food_day_rate_cents,
                   "clothing_day_rate_cents": event.clothing_day_rate_cents}))
    await db.commit()
    return await get_event(event.id, user, db)


@router.get("")
async def list_events(user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    events = (await db.execute(select(Event).order_by(Event.created_at.desc()))).scalars().all()
    return [{"id": e.id, "name": e.name, "slug": e.slug, "status": e.status, "venue": e.venue} for e in events]


@router.get("/{event_id}")
async def get_event(event_id: uuid.UUID, user: AuthUser = Depends(require_super_admin),
                    db: AsyncSession = Depends(get_db)):
    event = await _load_event(db, event_id)
    counts = dict((await db.execute(
        select(EventVendor.status, func.count()).where(EventVendor.event_id == event.id).group_by(EventVendor.status)
    )).all())
    money = dict((await db.execute(
        select(EventVendorSubscription.status, func.coalesce(func.sum(EventVendorSubscription.amount_cents), 0))
        .join(EventVendor, EventVendor.id == EventVendorSubscription.event_vendor_id)
        .where(EventVendor.event_id == event.id)
        .group_by(EventVendorSubscription.status)
    )).all())
    return {
        "id": event.id, "name": event.name, "slug": event.slug, "venue": event.venue, "timezone": event.timezone,
        "status": event.status, "prep_days": event.prep_days, "notes": event.notes,
        "registration_open": event.registration_open,
        "has_registration_code": event.registration_code_hash is not None,
        "food_day_rate_cents": event.food_day_rate_cents,
        "clothing_day_rate_cents": event.clothing_day_rate_cents,
        "days": [{"day": d.day, "is_billable": d.is_billable} for d in event.days],
        "billable_day_count": len(_billable(event)),
        "vendor_counts": counts,
        "active_vendors": counts.get("ACTIVE", 0),
        "revenue_paid_cents": int(money.get("PAID", 0)),       # VAT-inclusive
        "revenue_outstanding_cents": int(money.get("UNPAID", 0)),
    }


@router.patch("/{event_id}")
async def update_event(event_id: uuid.UUID, body: EventUpdate,
                       user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """Edit name, venue, notes or the preparation period. Rates are never editable here."""
    changes = body.model_dump(exclude_unset=True)
    if not changes or (changes.get("name", "x") is None) or (changes.get("prep_days", 0) is None):
        raise AppError("NO_VALID_FIELDS", "send at least one of name, venue, notes, prep_days "
                       "(name and prep_days cannot be null)", 422)
    event = await _load_event(db, event_id, lock=True)
    before = {k: getattr(event, k) for k in changes}
    for k, v in changes.items():
        setattr(event, k, v)
    db.add(_audit(user, "admin.event.updated", "event", event.id, {"from": before, "to": changes}))
    await db.flush()
    if "prep_days" in changes:
        await reconcile_event_vendor_access(db)  # the access window moved
    await db.commit()
    return await get_event(event.id, user, db)


@router.get("/{event_id}/revenue")
async def event_revenue(event_id: uuid.UUID, user: AuthUser = Depends(require_super_admin),
                        db: AsyncSession = Depends(get_db)):
    """Paid and outstanding totals (VAT-inclusive), per category and per billable day."""
    event = await _load_event(db, event_id)
    rows = (await db.execute(
        select(EventVendor, EventVendorSubscription)
        .join(EventVendorSubscription, EventVendorSubscription.event_vendor_id == EventVendor.id)
        .where(EventVendor.event_id == event.id)
    )).all()
    by_category = {c: {"paid_cents": 0, "outstanding_cents": 0, "vendors": 0} for c in ("FOOD", "CLOTHING")}
    for cat, n in (await db.execute(
            select(EventVendor.category, func.count()).where(EventVendor.event_id == event.id)
            .group_by(EventVendor.category))).all():
        by_category[cat]["vendors"] = int(n)
    paid_total = outstanding_total = 0
    billable = sorted(_billable(event))
    per_day = {d: {"vendors": set(), "cents": 0} for d in billable}
    for vendor, sub in rows:
        if sub.status == "PAID":
            by_category[vendor.category]["paid_cents"] += sub.amount_cents
            paid_total += sub.amount_cents
            for d in billable:
                if sub.starts_on <= d <= sub.ends_on:
                    per_day[d]["vendors"].add(vendor.id)
                    per_day[d]["cents"] += sub.day_rate_cents
        elif sub.status == "UNPAID":
            by_category[vendor.category]["outstanding_cents"] += sub.amount_cents
            outstanding_total += sub.amount_cents
    return {
        "paid_cents": paid_total, "outstanding_cents": outstanding_total, "by_category": by_category,
        "by_day": [{"day": d, "vendors_covered": len(per_day[d]["vendors"]),
                    "paid_cents_attributed": per_day[d]["cents"]} for d in billable],
    }


@router.post("/{event_id}/status")
async def set_event_status(event_id: uuid.UUID, body: EventStatusUpdate,
                           user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    event = await _load_event(db, event_id)
    previous = event.status
    event.status = body.status
    db.add(_audit(user, "admin.event.status_changed", "event", event.id, {"from": previous, "to": body.status}))
    await reconcile_event_vendor_access(db)
    await db.commit()
    return await get_event(event.id, user, db)


@router.post("/{event_id}/registration")
async def update_registration(event_id: uuid.UUID, body: RegistrationUpdate,
                              user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """
    Open or close vendor self-registration and optionally rotate the shared
    code. A new code is returned in plaintext in THIS response only; just its
    bcrypt hash is stored, and neither the code nor the hash is logged or
    audited. Opening an event that has no code yet generates one.
    """
    event = await _load_event(db, event_id, lock=True)
    code = None
    if body.rotate_code or (body.open and event.registration_code_hash is None):
        code = reg.generate_code()
        event.registration_code_hash = await reg.hash_code(code)
    event.registration_open = body.open
    db.add(_audit(user, "admin.event.registration_updated", "event", event.id,
                  {"open": body.open, "code_rotated": code is not None}))
    await db.commit()
    return {"registration_open": event.registration_open,
            "has_registration_code": event.registration_code_hash is not None,
            "registration_code": reg.display_code(code) if code else None}


@router.put("/{event_id}/calendar")
async def replace_calendar(event_id: uuid.UUID, body: CalendarUpdate,
                           user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """
    Publish or replace the event calendar. A billable day already inside a
    PAID subscription may not be removed or made non-billable, because that
    vendor has paid for it. Existing subscriptions keep their frozen price.
    """
    event = await _load_event(db, event_id, lock=True)
    new_days = {d.day: d.is_billable for d in body.days}
    if len(new_days) != len(body.days):
        raise AppError("DUPLICATE_EVENT_DAY", "each date may appear only once", 422)
    paid = (await db.execute(
        select(EventVendorSubscription.starts_on, EventVendorSubscription.ends_on)
        .join(EventVendor, EventVendor.id == EventVendorSubscription.event_vendor_id)
        .where(EventVendor.event_id == event.id, EventVendorSubscription.status == "PAID")
    )).all()
    for old in event.days:
        if old.is_billable and not new_days.get(old.day, False) and any(s <= old.day <= e for s, e in paid):
            raise AppError("PAID_DAY_REMOVED",
                           f"{old.day.isoformat()} is inside a paid vendor subscription and must stay billable", 409)
    previously_billable = {d.day for d in event.days if d.is_billable}
    for day, billable in new_days.items():
        if billable and day not in previously_billable and any(s <= day <= e for s, e in paid):
            # A paid range would gain a day nobody paid for.
            raise AppError("PAID_RANGE_GAINS_DAY",
                           f"{day.isoformat()} falls inside a paid vendor subscription; sell it as a new subscription", 409)
    for old in list(event.days):
        await db.delete(old)
    await db.flush()
    db.add_all([EventDay(event_id=event.id, day=d, is_billable=b) for d, b in new_days.items()])
    db.add(_audit(user, "admin.event.calendar_replaced", "event", event.id,
                  {"days": len(new_days), "billable": sum(new_days.values())}))
    await db.flush()
    db.expire(event)
    await reconcile_event_vendor_access(db)
    await db.commit()
    return await get_event(event.id, user, db)


# ── Vendors ───────────────────────────────────────────────────────────────────

async def _validate_rows(db: AsyncSession, event: Event, rows: list[VendorRow]) -> list[dict]:
    """Every row checked before anything is written. Returns the problems, one entry per bad row."""
    problems: list[dict] = []
    stalls = [r.stall_code for r in rows if r.stall_code]
    emails = [str(r.owner_email).lower() for r in rows if r.owner_email and not r.existing_business_id]
    existing_ids = [r.existing_business_id for r in rows if r.existing_business_id]
    dup_stalls, dup_emails = _dupes(stalls), _dupes(emails)
    dup_ids = _dupes([str(i) for i in existing_ids])
    taken_stalls = set((await db.execute(
        select(EventVendor.stall_code).where(EventVendor.event_id == event.id, EventVendor.stall_code.in_(stalls))
    )).scalars()) if stalls else set()
    for i, row in enumerate(rows):
        errs: list[str] = []
        if row.stall_code and (row.stall_code in dup_stalls or row.stall_code in taken_stalls):
            errs.append(f"stall {row.stall_code} is used more than once at this event")
        if row.existing_business_id:
            if str(row.existing_business_id) in dup_ids:
                errs.append("the same existing business appears more than once")
            biz = (await db.execute(select(Business).where(Business.id == row.existing_business_id))).scalar_one_or_none()
            if not biz:
                errs.append("existing_business_id not found")
            elif biz.plan != EVENT_PLAN:
                # Never move a permanent tenant onto event billing or event expiry.
                errs.append("existing business is not an event-plan vendor")
            elif (await db.execute(select(EventVendor.id).where(
                    EventVendor.event_id == event.id, EventVendor.business_id == biz.id))).scalar_one_or_none():
                errs.append("this business is already a vendor at this event")
        else:
            email = str(row.owner_email).lower()
            if email in dup_emails:
                errs.append("owner_email appears more than once in this import")
            else:
                try:
                    await ensure_owner_email_free(db, email)
                except AppError:
                    errs.append("owner_email already has a NextGen login (use existing_business_id for a returning vendor)")
        if row.starts_on:
            try:
                quote(row.category, event.food_day_rate_cents, event.clothing_day_rate_cents,
                      _billable(event), row.starts_on, row.ends_on)
            except ValueError as exc:
                errs.append(str(exc))
        if errs:
            problems.append({"row": i + 1, "errors": errs})
    return problems


@router.post("/{event_id}/vendors/bulk")
async def bulk_onboard_vendors(event_id: uuid.UUID, body: BulkVendorRequest,
                               user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """
    Onboard many vendors in one all-or-nothing transaction. Every row is
    validated first: one bad row means nothing is created. With dry_run, only
    the validation and the price quotes are returned. Owner temporary
    passwords are returned once, in this response only, and never logged.
    """
    event = await _load_event(db, event_id)
    if event.status == "CANCELLED":
        raise AppError("EVENT_CANCELLED", "cannot onboard vendors to a cancelled event", 409)
    problems = await _validate_rows(db, event, body.vendors)
    if problems:
        raise AppError("BULK_VALIDATION_FAILED", "no vendors were created", 422, details={"rows": problems})

    preview = []
    for row in body.vendors:
        q = _quote_or_422(event, row.category, row.starts_on, row.ends_on) if row.starts_on else None
        preview.append({"category": row.category, "stall_code": row.stall_code,
                        "amount_cents": q.amount_cents if q else None, "billable_days": q.billable_days if q else None})
    if body.dry_run:
        return {"dry_run": True, "vendors": preview}

    # bcrypt is ~1 s of CPU per hash. Hash every new owner's temporary password
    # concurrently in the threadpool BEFORE the (serial) database loop: 60 vendors
    # took ~95 s when each hash was awaited in turn. Passwords are returned once in
    # the response and never logged or stored in plaintext.
    new_rows = [r for r in body.vendors if not r.existing_business_id]
    temps = [generate_temp_password() for _ in new_rows]
    hashes = await asyncio.gather(*(run_in_threadpool(hash_password, t) for t in temps))
    credentials = {id(r): (t, h) for r, t, h in zip(new_rows, temps, hashes)}

    created: list[dict] = []
    try:
        for row, pv in zip(body.vendors, preview):
            temp_password = None
            if row.existing_business_id:
                biz = (await db.execute(select(Business).where(Business.id == row.existing_business_id))).scalar_one()
            else:
                provisioned = await provision_business(
                    db,
                    fields={"name": row.business_name, "plan": EVENT_PLAN, "industry": _CATEGORY_INDUSTRY[row.category],
                            "pos_enabled": True, "inventory_enabled": True, "currency": "ZAR",
                            "timezone": event.timezone,
                            # H1: WhatsApp orders bypass the trading gate; off until approved.
                            "is_whatsapp_enabled": False},
                    owner_email=str(row.owner_email),
                    owner_full_name=row.owner_full_name,
                    owner_temp_credentials=credentials[id(row)],
                )
                biz, temp_password = provisioned.business, provisioned.owner_temporary_password
            vendor = EventVendor(event_id=event.id, business_id=biz.id, category=row.category,
                                 stall_code=row.stall_code, status="PENDING")
            db.add(vendor)
            await db.flush()
            if row.starts_on:
                q = _quote_or_422(event, row.category, row.starts_on, row.ends_on)
                db.add(EventVendorSubscription(
                    event_vendor_id=vendor.id, business_id=biz.id, starts_on=row.starts_on, ends_on=row.ends_on,
                    day_rate_cents=q.day_rate_cents, billable_days=q.billable_days, amount_cents=q.amount_cents,
                    status="UNPAID",
                ))
            created.append({**pv, "vendor_id": vendor.id, "business_id": biz.id, "business_code": biz.business_code,
                            "owner_email": str(row.owner_email) if temp_password else None,
                            "owner_temporary_password": temp_password})
        db.add(_audit(user, "admin.event.vendors_bulk_onboarded", "event", event.id,
                      {"count": len(created), "new_businesses": sum(1 for c in created if c["owner_temporary_password"])}))
        await db.flush()
        await reconcile_event_vendor_access(db)
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent change (a stall, email or vendor taken
        # between validation and insert). Nothing was committed, and no
        # temporary password is returned for rows that do not exist.
        await db.rollback()
        raise AppError("BULK_CONFLICT", "another change took a stall, email or vendor slot; nothing was created", 409)
    return {"dry_run": False, "vendors": created}


_CSV_COLUMNS = {"category", "stall_code", "existing_business_id", "business_name", "owner_email",
                "owner_full_name", "starts_on", "ends_on"}


@router.post("/{event_id}/vendors/bulk-csv")
async def bulk_onboard_vendors_csv(event_id: uuid.UUID, body: BulkVendorCsvRequest,
                                   user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """The same import from a CSV with a header row (columns: see _CSV_COLUMNS); empty cells are omitted."""
    reader = csv.DictReader(io.StringIO(body.csv_text.lstrip("﻿")))
    try:
        fieldnames = reader.fieldnames
    except csv.Error as exc:
        raise AppError("CSV_INVALID", f"could not read the CSV: {exc}", 422)
    unknown = set(fieldnames or []) - _CSV_COLUMNS
    if not fieldnames or unknown:
        raise AppError("CSV_COLUMNS", f"unknown columns or no header row: {sorted(unknown)}", 422)
    if "category" not in fieldnames:
        raise AppError("CSV_COLUMNS", "the category column is required", 422)
    rows, problems = [], []
    try:
        raw_rows = list(enumerate(reader, start=1))
    except csv.Error as exc:
        raise AppError("CSV_INVALID", f"could not read the CSV: {exc}", 422)
    if len(raw_rows) > MAX_BULK_VENDORS:
        raise AppError("CSV_TOO_LARGE", f"at most {MAX_BULK_VENDORS} vendors per import", 422)
    for i, raw in raw_rows:
        data = {k: v.strip() for k, v in raw.items() if k and v and v.strip()}
        if "category" in data:
            data["category"] = data["category"].upper()
        try:
            rows.append(VendorRow(**data))
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            detail = ([f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in errors()]
                      if callable(errors) else [str(exc).splitlines()[0]])
            problems.append({"row": i, "errors": detail})
    if problems:
        raise AppError("BULK_VALIDATION_FAILED", "no vendors were created", 422, details={"rows": problems})
    if not rows:
        raise AppError("CSV_EMPTY", "the CSV has no vendor rows", 422)
    return await bulk_onboard_vendors(event_id, BulkVendorRequest(vendors=rows, dry_run=body.dry_run), user, db)


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/{event_id}/vendors")
async def list_vendors(
    event_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    status: Annotated[str | None, Query(pattern="^(PENDING|ACTIVE|UNPAID|EXPIRED|CANCELLED)$")] = None,
    category: Annotated[str | None, Query(pattern="^(FOOD|CLOTHING)$")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    event = await _load_event(db, event_id)
    conds = [EventVendor.event_id == event_id]
    if status:
        conds.append(EventVendor.status == status)
    if category:
        conds.append(EventVendor.category == category)
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        conds.append(or_(Business.name.ilike(like, escape="\\"),
                         EventVendor.stall_code.ilike(like, escape="\\"),
                         Business.business_code.ilike(like, escape="\\")))
    total = (await db.execute(
        select(func.count()).select_from(EventVendor).join(Business, Business.id == EventVendor.business_id)
        .where(*conds))).scalar_one()
    rows = (await db.execute(
        select(EventVendor, Business.name, Business.business_code, Business.is_active, Business.suspended_reason)
        .join(Business, Business.id == EventVendor.business_id)
        .where(*conds)
        .options(selectinload(EventVendor.subscriptions))
        .order_by(EventVendor.stall_code.nulls_last(), Business.name, EventVendor.id)
        .limit(limit).offset(offset)
    )).all()
    local_day = datetime.now(timezone.utc).astimezone(ZoneInfo(event.timezone)).date()
    billable = set(_billable(event))
    items = []
    for v, name, code, active, suspended_reason in rows:
        paid = [PaidRange(s.starts_on, s.ends_on) for s in v.subscriptions if s.status == "PAID"]
        dec = evaluate_access(local_day, billable, paid, event.prep_days,
                              cancelled=v.status == "CANCELLED" or event.status == "CANCELLED")
        items.append({
            "vendor_id": v.id, "business_id": v.business_id, "business_name": name, "business_code": code,
            "business_active": active, "suspended_reason": suspended_reason, "category": v.category,
            "stall_code": v.stall_code, "status": v.status,
            "access_level": dec.level, "access_reason": dec.reason,
            "subscriptions": [{"id": s.id, "starts_on": s.starts_on, "ends_on": s.ends_on,
                               "billable_days": s.billable_days, "amount_cents": s.amount_cents,
                               "status": s.status, "paid_at": s.paid_at,
                               "payment_claim_reference": s.payment_claim_reference,
                               "payment_claimed_at": s.payment_claimed_at}
                              for s in sorted(v.subscriptions, key=lambda s: s.starts_on)],
        })
    return {"items": items, "total": int(total)}


@router.patch("/{event_id}/vendors/{vendor_id}")
async def update_vendor(event_id: uuid.UUID, vendor_id: uuid.UUID, body: VendorUpdate,
                        user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """Allocate (or clear, with null) the vendor's stall. Unique per event."""
    if "stall_code" not in body.model_fields_set:
        raise AppError("NO_VALID_FIELDS", "send stall_code (null clears it)", 422)
    vendor = await _load_vendor(db, event_id, vendor_id)
    stall = body.stall_code.strip() if body.stall_code is not None else None
    if body.stall_code is not None and not stall:
        raise AppError("INVALID_STALL_CODE", "stall_code cannot be blank", 422)
    if stall and (await db.execute(select(EventVendor.id).where(
            EventVendor.event_id == event_id, EventVendor.stall_code == stall,
            EventVendor.id != vendor.id))).scalar_one_or_none():
        raise AppError("STALL_TAKEN", f"stall {stall} is already allocated at this event", 409)
    previous = vendor.stall_code
    vendor.stall_code = stall
    db.add(_audit(user, "admin.event.vendor_stall_changed", "event_vendor", vendor.id,
                  {"from": previous, "to": stall}, business_id=vendor.business_id))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError("STALL_TAKEN", f"stall {stall} is already allocated at this event", 409)
    return {"vendor_id": vendor.id, "stall_code": stall}


@router.post("/{event_id}/vendors/{vendor_id}/suspend")
async def suspend_vendor(event_id: uuid.UUID, vendor_id: uuid.UUID, body: VendorSuspend,
                         user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """
    Suspend the vendor's business with the reserved reason EVENT_ADMIN_SUSPENDED.
    The hourly reconcile never lifts it; only reinstate does. The free-text
    reason is kept in the audit trail, not on the business.
    """
    vendor = await _load_vendor(db, event_id, vendor_id)
    biz = (await db.execute(select(Business).where(Business.id == vendor.business_id).with_for_update())).scalar_one()
    if biz.plan != EVENT_PLAN:
        raise AppError("NOT_EVENT_VENDOR", "only event-plan businesses are managed here", 409)
    # An automatic EVENT_SUBSCRIPTION_ENDED suspension may be upgraded to an
    # administrative one, so reconcile can never lift it behind the admin's back
    # (security review L3). Any other suspension is left as it is.
    if not biz.is_active and biz.suspended_reason != EVENT_SUSPENSION_REASON:
        raise AppError("ALREADY_SUSPENDED", f"this business is already suspended ({biz.suspended_reason})", 409)
    biz.is_active = False
    biz.suspended_reason = EVENT_ADMIN_SUSPENSION_REASON
    db.add(_audit(user, "admin.event.vendor_suspended", "event_vendor", vendor.id, {"reason": body.reason},
                  business_id=biz.id))
    await db.commit()
    return {"vendor_id": vendor.id, "business_active": False, "suspended_reason": EVENT_ADMIN_SUSPENSION_REASON}


@router.post("/{event_id}/vendors/{vendor_id}/reinstate")
async def reinstate_vendor(event_id: uuid.UUID, vendor_id: uuid.UUID,
                           user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """Lift only EVENT_ADMIN_SUSPENDED, then reconcile (which may still suspend if the vendor has no access)."""
    vendor = await _load_vendor(db, event_id, vendor_id)
    biz = (await db.execute(select(Business).where(Business.id == vendor.business_id).with_for_update())).scalar_one()
    if biz.is_active or biz.suspended_reason != EVENT_ADMIN_SUSPENSION_REASON:
        raise AppError("NOT_ADMIN_SUSPENDED", "this business is not suspended by an event administrator", 409)
    biz.is_active = True
    biz.suspended_reason = None
    db.add(_audit(user, "admin.event.vendor_reinstated", "event_vendor", vendor.id, {}, business_id=biz.id))
    await db.flush()
    await reconcile_event_vendor_access(db)
    await db.commit()
    await db.refresh(biz)
    return {"vendor_id": vendor.id, "business_active": biz.is_active, "suspended_reason": biz.suspended_reason}


async def _load_vendor(db: AsyncSession, event_id: uuid.UUID, vendor_id: uuid.UUID,
                       lock: bool = False) -> EventVendor:
    stmt = select(EventVendor).where(EventVendor.id == vendor_id, EventVendor.event_id == event_id)
    if lock:
        stmt = stmt.with_for_update()
    vendor = (await db.execute(stmt)).scalar_one_or_none()
    if not vendor:
        raise NotFoundError("Event vendor", str(vendor_id))
    return vendor


@router.post("/{event_id}/vendors/{vendor_id}/subscriptions", status_code=201)
async def create_subscription(event_id: uuid.UUID, vendor_id: uuid.UUID, body: SubscriptionCreate,
                              user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """Quote a 1-day, multi-day or full-event subscription. Created UNPAID; mark it paid when payment arrives."""
    event = await _load_event(db, event_id, lock=True)
    vendor = await _load_vendor(db, event_id, vendor_id)
    if vendor.status == "CANCELLED":
        raise AppError("VENDOR_CANCELLED", "this vendor was cancelled", 409)
    q = _quote_or_422(event, vendor.category, body.starts_on, body.ends_on)
    await assert_no_overlap(db, vendor.id, body.starts_on, body.ends_on)
    sub = EventVendorSubscription(
        event_vendor_id=vendor.id, business_id=vendor.business_id, starts_on=body.starts_on, ends_on=body.ends_on,
        day_rate_cents=q.day_rate_cents, billable_days=q.billable_days, amount_cents=q.amount_cents, status="UNPAID",
    )
    db.add(sub)
    await db.flush()
    db.add(_audit(user, "admin.event.subscription_quoted", "event_vendor_subscription", sub.id,
                  {"amount_cents": q.amount_cents, "billable_days": q.billable_days}, business_id=vendor.business_id))
    await db.commit()
    return {"id": sub.id, "status": sub.status, "billable_days": sub.billable_days, "amount_cents": sub.amount_cents}


async def _load_subscription(db, event_id, subscription_id) -> EventVendorSubscription:
    sub = (await db.execute(
        select(EventVendorSubscription)
        .join(EventVendor, EventVendor.id == EventVendorSubscription.event_vendor_id)
        .where(EventVendorSubscription.id == subscription_id, EventVendor.event_id == event_id)
        .with_for_update()
    )).scalar_one_or_none()
    if not sub:
        raise NotFoundError("Event subscription", str(subscription_id))
    return sub


@router.post("/{event_id}/subscriptions/{subscription_id}/mark-paid")
async def mark_subscription_paid(event_id: uuid.UUID, subscription_id: uuid.UUID, body: MarkPaid,
                                 user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    """Record payment. Idempotent for the same reference. Access is applied immediately, not at the next hourly run."""
    # Serialize with calendar replace / quoting for this event: lock the event
    # row first (always event, then subscription, so lock order is consistent).
    event = await _load_event(db, event_id, lock=True)
    sub = await _load_subscription(db, event_id, subscription_id)
    if sub.status == "PAID":
        if sub.payment_reference is not None and normalise_payment_reference(sub.payment_reference) == body.payment_reference:
            return {"id": sub.id, "status": "PAID", "already_paid": True}
        raise AppError("ALREADY_PAID", "this subscription is already paid under a different reference", 409)
    if sub.status == "VOID":
        raise AppError("SUBSCRIPTION_VOID", "a void subscription cannot be paid", 409)
    # The calendar may have changed since this was quoted: the price must
    # still match today's calendar, or it is re-quoted first (review M4).
    vendor = (await db.execute(select(EventVendor).where(EventVendor.id == sub.event_vendor_id))).scalar_one()
    try:
        current = quote(vendor.category, sub.day_rate_cents, sub.day_rate_cents, _billable(event),
                        sub.starts_on, sub.ends_on)
    except ValueError:
        current = None
    if current is None or current.billable_days != sub.billable_days or current.amount_cents != sub.amount_cents:
        raise AppError("QUOTE_STALE",
                       "the event calendar changed since this subscription was quoted; void it and quote again", 409)
    if await payment_reference_in_use(db, body.payment_reference, exclude_id=sub.id):
        raise AppError("PAYMENT_REFERENCE_USED", "this payment reference already settles another subscription", 409)
    sub.status = "PAID"
    sub.paid_at = datetime.now(timezone.utc)
    sub.payment_reference = body.payment_reference
    db.add(_audit(user, "admin.event.subscription_paid", "event_vendor_subscription", sub.id,
                  {"amount_cents": sub.amount_cents}, business_id=sub.business_id))
    try:
        await db.flush()
    except IntegrityError as exc:
        # Another event's mark-paid used the same reference at the same moment
        # (the partial unique index is the cross-event backstop). Any other
        # integrity error is a real bug and must not be reported as this one.
        await db.rollback()
        if not is_payment_reference_violation(exc):
            raise
        raise AppError("PAYMENT_REFERENCE_USED", "this payment reference already settles another subscription", 409)
    await reconcile_event_vendor_access(db)
    await db.commit()
    return {"id": sub.id, "status": "PAID", "already_paid": False}


@router.post("/{event_id}/subscriptions/{subscription_id}/void")
async def void_subscription(event_id: uuid.UUID, subscription_id: uuid.UUID,
                            user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    sub = await _load_subscription(db, event_id, subscription_id)
    if sub.status == "PAID":
        # Refunds are a money movement, not a status flip; not done here.
        raise AppError("SUBSCRIPTION_PAID", "a paid subscription cannot be voided here; refund it instead", 409)
    sub.status = "VOID"
    db.add(_audit(user, "admin.event.subscription_voided", "event_vendor_subscription", sub.id, {},
                  business_id=sub.business_id))
    await db.commit()
    return {"id": sub.id, "status": "VOID"}


@router.post("/{event_id}/vendors/{vendor_id}/cancel")
async def cancel_vendor(event_id: uuid.UUID, vendor_id: uuid.UUID,
                        user: AuthUser = Depends(require_super_admin), db: AsyncSession = Depends(get_db)):
    # Row lock: a concurrent change to this vendor (a reconcile write, another
    # cancel) cannot interleave with the status flip (security review L1).
    vendor = await _load_vendor(db, event_id, vendor_id, lock=True)
    vendor.status = "CANCELLED"
    vendor.status_changed_at = datetime.now(timezone.utc)
    db.add(_audit(user, "admin.event.vendor_cancelled", "event_vendor", vendor.id, {}, business_id=vendor.business_id))
    await db.flush()
    await reconcile_event_vendor_access(db)
    await db.commit()
    return {"vendor_id": vendor.id, "status": "CANCELLED"}
