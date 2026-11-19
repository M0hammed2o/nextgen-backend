"""
Real-Postgres scale and lifecycle verification for the souk event product.

What this file proves (see each test's docstring):
  1. Bulk onboarding of 60 mixed FOOD/CLOTHING vendors in ONE admin call (the real
     route function, real session), the per-call cap, and all-or-nothing behaviour.
  2. A controlled-clock lifecycle across 60 vendors: before the prep window, inside it,
     day 1, day 3, after the event, reinstatement by paying an extra day, admin
     suspension never lifted by reconcile, and reconcile idempotency.
  3. Tenant isolation across 60 vendors that share the very same SKU and barcode.
  4. Food (menu -> POS sale with ready-notification consent -> kitchen -> COMPLETED) and
     clothing (variant matrix -> barcode lookup -> sale -> line return with restock ->
     exchange) workflows.

Clock control. No production code was changed to make time injectable: the only clock
read in the access path is ``datetime.now`` inside ``backend.app.events.service``
(reconcile and ``business_access_decision``, which the POS/order trading gate calls).
The ``clock`` fixture replaces that one module-level name with a subclass whose ``now``
returns a controlled instant, so reconcile, the admin routes (which reconcile after
every payment) and the trading gate all see the same controlled time. The event is
fixed in March 2030 (Africa/Johannesburg), far from the real clock.

No real WhatsApp/payment message is ever sent: ``send_notification_message`` is patched
wherever a send path is reachable.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from admin_api.app.api.v1 import routes_events as admin_events
from admin_api.app.api.v1.routes_events import (
    BulkVendorCsvRequest,
    BulkVendorRequest,
    EventCreate,
    EventDayIn,
    MarkPaid,
    SubscriptionCreate,
    VendorRow,
    VendorSuspend,
    bulk_onboard_vendors,
    bulk_onboard_vendors_csv,
    cancel_vendor,
    create_event,
    create_subscription,
    list_vendors,
    mark_subscription_paid,
    reinstate_vendor,
    suspend_vendor,
)
from backend.app.api.v1 import routes_event_vendor as ven
from backend.app.api.v1.routes_menu import (
    CategoryCreate,
    ItemCreate,
    ItemUpdate,
    create_category,
    create_item,
    list_items,
    soft_delete_item,
    update_item,
)
from backend.app.api.v1.routes_orders import (
    PaymentUpdateRequest,
    StatusUpdateRequest,
    get_order,
    list_orders,
    update_order_status,
    update_payment_status,
)
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSRefundRequest,
    POSReturnLine,
    TillOpenRequest,
    create_pos_order,
    open_till,
    refund_pos_order,
)
from backend.app.api.v1.routes_variants import (
    MatrixCreate,
    VariantCreate,
    VariantUpdate,
    create_variant,
    create_variant_matrix,
    deactivate_variant,
    list_variants,
    pos_lookup,
    update_variant,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.security import verify_password
from backend.app.events import service as event_service
from backend.app.events.rules import CONFIGURE, NONE, TRADE
from backend.app.events.service import (
    EVENT_ADMIN_SUSPENSION_REASON,
    EVENT_SUSPENSION_REASON,
    business_access_decision,
    reconcile_event_vendor_access,
)
from backend.app.services.auth_service import AuthService
from shared.enums import EVENT_PLAN
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.event import Event, EventVendor, EventVendorSubscription
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderEvent
from shared.models.user import BusinessUser
from shared.models.variant import ProductVariant
from tests.integration.test_product_variants_realdb import _checkout, _sell, _stock  # noqa: F401
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    ADMIN,
    _user,
    bag,
)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
D = [date(2030, 3, 10) + timedelta(days=i) for i in range(5)]  # five billable days
# prep_days=14: the prep window opens 2030-02-24.
T_BEFORE = datetime(2030, 2, 1, 10, 0, tzinfo=UTC)
T_PREP = datetime(2030, 3, 1, 10, 0, tzinfo=UTC)
T_DAY1 = datetime(2030, 3, 10, 10, 0, tzinfo=UTC)
T_DAY3 = datetime(2030, 3, 12, 10, 0, tzinfo=UTC)
T_AFTER = datetime(2030, 3, 15, 10, 0, tzinfo=UTC)


# -- clock ---------------------------------------------------------------------

class _Clock:
    now = T_BEFORE


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    c.now = T_BEFORE

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now if tz is None else c.now.astimezone(tz)

    monkeypatch.setattr(event_service, "datetime", FrozenDatetime)
    return c


# -- helpers -------------------------------------------------------------------

async def _event(db, bag, days=D, **kw) -> dict:
    ev = await create_event(
        EventCreate(name=f"Scale Souk {uuid.uuid4().hex[:6]}", days=[EventDayIn(day=d) for d in days], **kw),
        ADMIN, db)
    bag["events"].append(ev["id"])
    return ev


def _row(bag, category="FOOD", name=None, stall=None, starts=None, ends=None, email=None) -> VendorRow:
    email = email or f"sc-{uuid.uuid4().hex[:10]}@example.com"
    bag["emails"].append(email)
    kw = dict(category=category, stall_code=stall, business_name=name or f"Scale Stall {uuid.uuid4().hex[:6]}",
              owner_email=email, owner_full_name="Vendor Owner")
    if starts:
        kw.update(starts_on=starts, ends_on=ends or starts)
    return VendorRow(**kw)


async def _refused(coro, *, status: int | None = None) -> str:
    with pytest.raises((AppError, NotFoundError)) as exc:
        await coro
    if status is not None:
        assert exc.value.status_code == status, (exc.value.status_code, getattr(exc.value, "code", None))
    return getattr(exc.value, "code", None) or getattr(exc.value, "error_code", None)


# cohorts of the 60-vendor scenario
FULL, PARTIAL, UNPAID, CANCELLED = range(4)
COHORT_SIZES = {FULL: 40, PARTIAL: 10, UNPAID: 5, CANCELLED: 5}


def _scale_rows(bag) -> tuple[list[VendorRow], list[int]]:
    rows, cohorts = [], []
    for cohort, n in COHORT_SIZES.items():
        for _ in range(n):
            i = len(rows)
            ends = D[1] if cohort == PARTIAL else D[4]
            rows.append(_row(bag, "FOOD" if i % 2 == 0 else "CLOTHING", stall=f"S-{i:03d}", starts=D[0], ends=ends))
            cohorts.append(cohort)
    return rows, cohorts


async def _onboard_scale(db, bag, ev):
    """Onboard 60 vendors in one call. Returns (vendors, cohort_of(vendor), elapsed_seconds)."""
    rows, cohorts = _scale_rows(bag)
    t0 = time.perf_counter()
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    elapsed = time.perf_counter() - t0
    vendors = res["vendors"]
    for v, c, r in zip(vendors, cohorts, rows):
        v["cohort"] = c
        v["category"] = r.category
    return vendors, elapsed


def _ids(vendors, *cohorts):
    return [v["business_id"] for v in vendors if not cohorts or v["cohort"] in cohorts]


def _of(vendors, *cohorts):
    return [v for v in vendors if v["cohort"] in cohorts]


async def _sub_ids(db, ev_id) -> dict:
    rows = (await db.execute(
        select(EventVendorSubscription.event_vendor_id, EventVendorSubscription.id)
        .join(EventVendor, EventVendor.id == EventVendorSubscription.event_vendor_id)
        .where(EventVendor.event_id == ev_id))).all()
    return {r[0]: r[1] for r in rows}


async def _pay(db, ev, sub_id):
    return await mark_subscription_paid(
        ev["id"], sub_id, MarkPaid(payment_reference=f"ref-{uuid.uuid4().hex}"), ADMIN, db)


async def _snapshot(db, ids):
    biz = (await db.execute(select(Business.id, Business.is_active, Business.suspended_reason)
                            .where(Business.id.in_(ids)))).all()
    ven_ = (await db.execute(select(EventVendor.business_id, EventVendor.status, EventVendor.status_changed_at)
                             .where(EventVendor.business_id.in_(ids)))).all()
    return {r[0]: (r[1], r[2]) for r in biz}, {r[0]: (r[1], r[2]) for r in ven_}


async def _reconcile(db, clock, at, ids):
    """Reconcile at `at`, then again: the second run must change nothing at all."""
    clock.now = at
    db.expire_all()
    first = await reconcile_event_vendor_access(db, now=at)
    await db.commit()
    before = await _snapshot(db, ids)
    db.expire_all()
    again = await reconcile_event_vendor_access(db, now=at)
    await db.commit()
    assert (again.vendor_status_changes, again.businesses_suspended, again.businesses_reactivated) == (0, [], [])
    assert await _snapshot(db, ids) == before, "reconcile is not idempotent"
    return first


async def _levels(db, vendors, at):
    """{business_id: (level, reason)} from the live access decision."""
    out = {}
    for v in vendors:
        d = await business_access_decision(db, v["business_id"], at)
        out[v["business_id"]] = (d.level, d.reason)
    return out


async def _assert_levels(db, vendors, at, level, reason=None):
    got = await _levels(db, vendors, at)
    bad = {k: x for k, x in got.items() if x[0] != level or (reason and x[1] != reason)}
    assert not bad, f"expected {level}/{reason}, got {Counter(got.values())}"


async def _assert_biz(db, vendors, *, active: bool, reason):
    biz, _ = await _snapshot(db, [v["business_id"] for v in vendors])
    got = Counter(biz.values())
    assert got == Counter({(active, reason): len(vendors)}), got


async def _assert_vendor_status(db, vendors, status):
    _, ven_ = await _snapshot(db, [v["business_id"] for v in vendors])
    got = Counter(s for s, _ in ven_.values())
    assert got == Counter({status: len(vendors)}), got


async def _login_code(db, vendor) -> str | None:
    """Real AuthService.login_email with the one-time temporary password."""
    try:
        await AuthService(db).login_email(vendor["owner_email"], vendor["owner_temporary_password"])
    except AppError as exc:
        return exc.code
    return None  # would mean tokens were issued despite must_change_password


async def _login_probe(db, vendors, expected_code):
    # PASSWORD_CHANGE_REQUIRED is raised only AFTER the credentials and the business-active check
    # pass, so it proves "may log in"; BUSINESS_SUSPENDED proves the suspension is enforced.
    for v in vendors:
        assert await _login_code(db, v) == expected_code, v["owner_email"]


async def _shop(db, owner, requires_prep=True, price=5000, name="Stall Item"):
    cat = await create_category(body=CategoryCreate(name="Goods"), user=owner, db=db)
    item = await create_item(body=ItemCreate(category_id=cat.id, name=name, price_cents=price,
                                             requires_preparation=requires_prep), user=owner, db=db)
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    return item


async def _setup_shops(db, vendors) -> dict:
    items = {}
    for v in vendors:
        owner = _user(v["business_id"])
        items[v["business_id"]] = (await _shop(db, owner)).id
    return items


async def _ring(db, v, item_id, **kw):
    return await create_pos_order(
        POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item_id, quantity=1)],
                              payment_method="CASH", cash_tendered_cents=5000, **kw),
        _user(v["business_id"]), db)


async def _order_count(db, ids) -> int:
    return int((await db.execute(select(func.count()).select_from(Order).where(Order.business_id.in_(ids)))).scalar())


async def _sales_succeed(db, vendors, items) -> list:
    out = []
    for v in vendors:
        o = await _ring(db, v, items[v["business_id"]])
        assert (o.payment_status, o.total_cents) == ("PAID", 5000)
        out.append(o)
    return out


async def _sales_refused(db, vendors, items):
    for v in vendors:
        code = await _refused(_ring(db, v, items[v["business_id"]]), status=403)
        assert code == "EVENT_TRADING_NOT_ALLOWED", code


# ==============================================================================
# 1. Bulk onboarding
# ==============================================================================

async def test_bulk_onboarding_60_mixed_vendors_in_one_admin_call(db, bag, clock, capsys):
    """Uses the real route function bulk_onboard_vendors (same direct-call pattern as the other event files)."""
    ev = await _event(db, bag)
    vendors, elapsed = await _onboard_scale(db, bag, ev)
    with capsys.disabled():
        print(f"\n[scale] 60-vendor bulk onboarding wall time: {elapsed:.2f}s (bcrypt in threadpool)")
    assert len(vendors) == 60
    ids = [v["business_id"] for v in vendors]
    assert len(set(ids)) == 60 and len({v["vendor_id"] for v in vendors}) == 60

    biz = {b.id: b for b in (await db.execute(select(Business).where(Business.id.in_(ids)))).scalars()}
    assert len(biz) == 60
    assert len({b.slug for b in biz.values()}) == 60
    assert len({b.business_code for b in biz.values()}) == 60
    assert all(v["business_code"] == biz[v["business_id"]].business_code for v in vendors)
    for v in vendors:
        b = biz[v["business_id"]]
        assert b.industry == {"FOOD": "EVENT_FOOD", "CLOTHING": "EVENT_RETAIL"}[v["category"]]
        assert b.plan == EVENT_PLAN == "EVENT"
        assert b.is_whatsapp_enabled is False
        assert b.whatsapp_phone_number_id is None and b.whatsapp_business_account_id is None
        assert (b.payment_provider, b.payment_api_key, b.payment_api_secret, b.payment_webhook_secret) == (None,) * 4
        assert (b.stripe_customer_id, b.stripe_subscription_id, b.payfast_subscription_token) == (None,) * 3
        assert b.pos_enabled is True and b.inventory_enabled is True
    assert Counter(b.industry for b in biz.values()) == {"EVENT_FOOD": 30, "EVENT_RETAIL": 30}

    owners = (await db.execute(select(BusinessUser).where(BusinessUser.business_id.in_(ids)))).scalars().all()
    assert len(owners) == 60 and len({o.business_id for o in owners}) == 60  # exactly one owner per business
    assert len({o.email for o in owners}) == 60
    assert all(o.role == "OWNER" and o.must_change_password is True and o.is_active for o in owners)
    passwords = [v["owner_temporary_password"] for v in vendors]
    assert all(passwords) and len(set(passwords)) == 60
    assert len({o.password_hash for o in owners}) == 60
    by_email = {o.email: o for o in owners}
    t0 = time.perf_counter()
    ok = await asyncio.gather(*[
        run_in_threadpool(verify_password, v["owner_temporary_password"], by_email[v["owner_email"].lower()].password_hash)
        for v in vendors])
    assert all(ok), "each owner's own temporary password must verify against that owner's hash"
    # ... and a different vendor's password must not.
    assert not verify_password(vendors[0]["owner_temporary_password"], by_email[vendors[1]["owner_email"].lower()].password_hash)
    with capsys.disabled():
        print(f"[scale] verifying all 60 hashes (parallel threadpool): {time.perf_counter() - t0:.2f}s")

    evs = (await db.execute(select(EventVendor).where(EventVendor.event_id == ev["id"]))).scalars().all()
    assert len(evs) == 60 and {e.business_id for e in evs} == set(ids)
    assert Counter(e.category for e in evs) == {"FOOD": 30, "CLOTHING": 30}
    assert len({e.stall_code for e in evs}) == 60

    # The one-time passwords are returned once and never persisted anywhere reachable: not in audit rows.
    audit = (await db.execute(select(AuditEvent.diff_json).where(
        (AuditEvent.target_id == uuid.UUID(str(ev["id"]))) | AuditEvent.business_id.in_(ids)))).scalars().all()
    blob = repr(audit)
    assert not any(p in blob for p in passwords)

    # Paging over the 60 (admin list is capped at 200 per page, default 50).
    p1 = await list_vendors(ev["id"], ADMIN, db, limit=50, offset=0)
    p2 = await list_vendors(ev["id"], ADMIN, db, limit=50, offset=50)
    assert (p1["total"], len(p1["items"]), len(p2["items"])) == (60, 50, 10)
    assert {i["vendor_id"] for i in p1["items"]} | {i["vendor_id"] for i in p2["items"]} == {v["vendor_id"] for v in vendors}


async def test_bulk_cap_is_100_and_101_is_refused(db, bag, clock):
    ev = await _event(db, bag)
    assert admin_events.MAX_BULK_VENDORS == 100
    with pytest.raises(ValidationError):
        BulkVendorRequest(vendors=[_row(bag) for _ in range(101)])
    # 100 passes validation (dry run: quotes only, nothing written).
    out = await bulk_onboard_vendors(
        ev["id"], BulkVendorRequest(vendors=[_row(bag, starts=D[0], ends=D[1]) for _ in range(100)], dry_run=True), ADMIN, db)
    assert out["dry_run"] is True and len(out["vendors"]) == 100
    assert (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar() == 0
    # CSV path enforces the same cap.
    header = "category,business_name,owner_email\n"
    csv101 = header + "".join(f"FOOD,Csv Stall {i},csv-{uuid.uuid4().hex[:8]}@example.com\n" for i in range(101))
    assert await _refused(bulk_onboard_vendors_csv(ev["id"], BulkVendorCsvRequest(csv_text=csv101), ADMIN, db)) == "CSV_TOO_LARGE"


async def test_bulk_exactly_100_vendors_succeeds_in_one_call(db, bag, clock, capsys):
    ev = await _event(db, bag)
    rows = [_row(bag, "FOOD" if i % 2 else "CLOTHING", stall=f"M-{i:03d}") for i in range(100)]
    t0 = time.perf_counter()
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    elapsed = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[scale] 100-vendor (cap) bulk onboarding wall time: {elapsed:.2f}s")
    vs = res["vendors"]
    assert len(vs) == 100 and len({v["business_id"] for v in vs}) == 100
    assert len({v["owner_temporary_password"] for v in vs}) == 100


async def test_bulk_batch_with_one_duplicate_email_is_all_or_nothing(db, bag, clock):
    ev = await _event(db, bag)
    rows = [_row(bag) for _ in range(59)]
    dup = _row(bag, email=str(rows[9].owner_email).upper())  # same email, different case, row 60
    rows.append(dup)
    names = [str(r.business_name) for r in rows]
    emails = [str(r.owner_email).lower() for r in rows]
    with pytest.raises(AppError) as exc:
        await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    assert exc.value.code == "BULK_VALIDATION_FAILED" and exc.value.status_code == 422
    assert [r["row"] for r in exc.value.details["rows"]] == [10, 60]  # both occurrences are named
    await db.rollback()
    assert (await db.execute(select(func.count()).select_from(Business).where(Business.name.in_(names)))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(BusinessUser).where(BusinessUser.email.in_(emails)))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar() == 0

    # An email that already has a NextGen login anywhere on the platform is refused the same way.
    [first] = (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[_row(bag)]), ADMIN, db))["vendors"]
    batch = [_row(bag) for _ in range(20)] + [_row(bag, email=first["owner_email"])]
    assert await _refused(bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=batch), ADMIN, db)) == "BULK_VALIDATION_FAILED"
    await db.rollback()
    assert (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar() == 1


async def test_bulk_failure_after_rows_were_flushed_leaves_nothing_behind(db, bag, clock):
    """Row 31 blows up after 30 businesses/owners/vendors were flushed in the same transaction."""
    ev = await _event(db, bag)
    rows = [_row(bag, stall=f"F-{i:02d}") for i in range(40)]
    names = [str(r.business_name) for r in rows]
    emails = [str(r.owner_email).lower() for r in rows]
    real = admin_events.provision_business
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 31:
            raise RuntimeError("simulated crash mid-import")
        return await real(*a, **kw)

    with patch.object(admin_events, "provision_business", new=flaky):
        with pytest.raises(RuntimeError):
            await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    await db.rollback()  # what the request-scoped session does when the request fails
    assert calls["n"] == 31
    assert (await db.execute(select(func.count()).select_from(Business).where(Business.name.in_(names)))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(BusinessUser).where(BusinessUser.email.in_(emails)))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar() == 0


async def test_bulk_race_on_unique_email_after_validation_is_a_clean_409_and_nothing_is_created(db, bag, clock):
    """
    A concurrent-request race: the duplicate email slips past route validation. Two layers still refuse it,
    each with a 409 and nothing committed: (1) provisioning's own per-row email check, (2) the database unique
    index when that check is also bypassed (the route turns the IntegrityError into BULK_CONFLICT).
    """
    from admin_api.app.services import provisioning

    ev = await _event(db, bag)
    rows = [_row(bag) for _ in range(12)]
    rows[9] = _row(bag, email=str(rows[3].owner_email))  # duplicate email, row 10
    names = [str(r.business_name) for r in rows]

    async def nothing_created():
        await db.rollback()
        assert (await db.execute(select(func.count()).select_from(Business).where(Business.name.in_(names)))).scalar() == 0
        assert (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar() == 0

    with patch.object(admin_events, "_validate_rows", new=AsyncMock(return_value=[])):
        with pytest.raises(AppError) as exc:
            await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
        assert (exc.value.code, exc.value.status_code) == ("USER_DUPLICATE", 409)
        await nothing_created()
        with patch.object(provisioning, "ensure_owner_email_free", new=AsyncMock(return_value=None)):
            with pytest.raises(AppError) as exc:
                await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
        assert (exc.value.code, exc.value.status_code) == ("BULK_CONFLICT", 409)
        await nothing_created()


async def test_many_vendors_with_the_same_business_name_get_distinct_slugs(db, bag, clock):
    """A real souk has many 'Burgers' / 'Vintage Tees'. 30 identical names must still onboard."""
    ev = await _event(db, bag)
    rows = [_row(bag, name="Souk Burgers") for _ in range(30)]
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
    ids = [v["business_id"] for v in res["vendors"]]
    slugs = (await db.execute(select(Business.slug).where(Business.id.in_(ids)))).scalars().all()
    assert len(set(slugs)) == 30


# ==============================================================================
# 2. Lifecycle with a controlled clock
# ==============================================================================

async def test_60_vendor_lifecycle_with_controlled_clock(db, bag, clock):
    ev = await _event(db, bag)  # prep_days defaults to 14, five billable days
    assert ev["prep_days"] == 14 and ev["billable_day_count"] == 5
    vendors, _ = await _onboard_scale(db, bag, ev)
    everyone = _ids(vendors)
    full, partial, unpaid, cancelled = (_of(vendors, c) for c in (FULL, PARTIAL, UNPAID, CANCELLED))
    assert (len(full), len(partial), len(unpaid), len(cancelled)) == (40, 10, 5, 5)
    traders = full + partial
    nonpaying = unpaid + cancelled

    subs = await _sub_ids(db, ev["id"])
    assert len(subs) == 60
    for v in full + partial + cancelled:  # 40 full, 10 days 1-2, and the 5 cancelled hold a PAID sub too
        await _pay(db, ev, subs[v["vendor_id"]])
    for v in cancelled:
        await cancel_vendor(ev["id"], v["vendor_id"], ADMIN, db)
    items = await _setup_shops(db, vendors)

    # -- A. before the prep window --------------------------------------------
    await _reconcile(db, clock, T_BEFORE, everyone)
    await _assert_biz(db, vendors, active=False, reason=EVENT_SUSPENSION_REASON)
    await _assert_levels(db, traders + unpaid, T_BEFORE, NONE, "BEFORE_PREP_WINDOW")
    await _assert_levels(db, cancelled, T_BEFORE, NONE, "CANCELLED")
    await _assert_vendor_status(db, traders + unpaid, "PENDING")
    await _assert_vendor_status(db, cancelled, "CANCELLED")
    await _login_probe(db, [full[0], partial[0], unpaid[0], cancelled[0]], "BUSINESS_SUSPENDED")

    # -- B. inside the prep window: configure only -------------------------------
    await _reconcile(db, clock, T_PREP, everyone)
    await _assert_biz(db, traders + unpaid, active=True, reason=None)
    await _assert_biz(db, cancelled, active=False, reason=EVENT_SUSPENSION_REASON)
    await _assert_levels(db, traders + unpaid, T_PREP, CONFIGURE, "PREP_WINDOW")
    await _assert_levels(db, cancelled, T_PREP, NONE, "CANCELLED")
    await _assert_vendor_status(db, traders, "ACTIVE")      # paid cover ahead
    await _assert_vendor_status(db, unpaid, "PENDING")
    await _login_probe(db, [full[0], full[1], partial[0], unpaid[0], unpaid[1]], "PASSWORD_CHANGE_REQUIRED")
    await _login_probe(db, [cancelled[0], cancelled[1]], "BUSINESS_SUSPENDED")
    await _sales_refused(db, vendors, items)  # paid, unpaid and cancelled alike: nobody trades in prep
    assert await _order_count(db, everyone) == 0

    # -- C. day 1 ---------------------------------------------------------------
    await _reconcile(db, clock, T_DAY1, everyone)
    await _assert_levels(db, traders, T_DAY1, TRADE, "PAID_DAY")
    await _assert_levels(db, unpaid, T_DAY1, CONFIGURE, "NO_PAID_COVER_TODAY")
    await _assert_levels(db, cancelled, T_DAY1, NONE, "CANCELLED")
    await _assert_biz(db, traders + unpaid, active=True, reason=None)
    await _assert_biz(db, cancelled, active=False, reason=EVENT_SUSPENSION_REASON)
    await _assert_vendor_status(db, traders, "ACTIVE")
    await _assert_vendor_status(db, unpaid, "UNPAID")
    orders = await _sales_succeed(db, traders, items)   # 50 real POS sales
    assert len(orders) == 50
    assert {o.order_number for o in orders} and len({o.id for o in orders}) == 50
    await _sales_refused(db, nonpaying, items)
    assert await _order_count(db, _ids(vendors, FULL, PARTIAL)) == 50
    assert await _order_count(db, _ids(vendors, UNPAID, CANCELLED)) == 0
    per_biz = dict((await db.execute(select(Order.business_id, func.count()).where(Order.business_id.in_(everyone))
                                     .group_by(Order.business_id))).all())
    assert set(per_biz) == {v["business_id"] for v in traders} and set(per_biz.values()) == {1}

    # -- D. day 3: the ten days-1-2 vendors lose trading ------------------------
    await _reconcile(db, clock, T_DAY3, everyone)
    await _assert_levels(db, full, T_DAY3, TRADE, "PAID_DAY")
    await _assert_levels(db, partial, T_DAY3, CONFIGURE, "NO_PAID_COVER_TODAY")
    await _assert_biz(db, traders + unpaid, active=True, reason=None)  # still allowed to log in and configure
    await _assert_vendor_status(db, partial, "EXPIRED")
    await _assert_vendor_status(db, full, "ACTIVE")
    await _sales_refused(db, partial + unpaid, items)
    await _login_probe(db, [partial[0], partial[1]], "PASSWORD_CHANGE_REQUIRED")
    await _sales_succeed(db, full, items)

    # Reinstatement by paying an extra day (admin quotes day 3, marks it paid): trading resumes at once.
    for v in partial:
        q = await create_subscription(ev["id"], v["vendor_id"], SubscriptionCreate(starts_on=D[2], ends_on=D[2]), ADMIN, db)
        assert q["billable_days"] == 1 and q["amount_cents"] in (10_000, 5_000)
        await _pay(db, ev, q["id"])
    await _assert_levels(db, partial, T_DAY3, TRADE, "PAID_DAY")
    await _assert_vendor_status(db, partial, "ACTIVE")
    await _sales_succeed(db, partial, items)
    await _sales_refused(db, unpaid, items)  # the extra day did not leak to anyone else
    assert await _order_count(db, _ids(vendors, FULL, PARTIAL)) == 100
    await _reconcile(db, clock, T_DAY3, everyone)

    # -- E. after the event: everyone suspended -------------------------------
    orders_before = await _order_count(db, everyone)
    items_before = (await db.execute(select(func.count()).select_from(MenuItem).where(MenuItem.business_id.in_(everyone)))).scalar()
    await _reconcile(db, clock, T_AFTER, everyone)
    await _assert_biz(db, vendors, active=False, reason=EVENT_SUSPENSION_REASON)
    await _assert_levels(db, traders + unpaid, T_AFTER, NONE, "EVENT_ENDED")
    await _assert_levels(db, cancelled, T_AFTER, NONE, "CANCELLED")
    await _assert_vendor_status(db, traders, "EXPIRED")
    await _assert_vendor_status(db, unpaid, "UNPAID")
    await _assert_vendor_status(db, cancelled, "CANCELLED")
    await _sales_refused(db, vendors, items)
    await _login_probe(db, [full[0], partial[0], unpaid[0], cancelled[0]], "BUSINESS_SUSPENDED")
    # Late payment after the event does not revive anyone.
    for v in unpaid:
        await _pay(db, ev, subs[v["vendor_id"]])
    await _assert_biz(db, unpaid, active=False, reason=EVENT_SUSPENSION_REASON)
    await _assert_levels(db, unpaid, T_AFTER, NONE, "EVENT_ENDED")
    await _sales_refused(db, unpaid, items)
    await _reconcile(db, clock, T_AFTER, everyone)
    # Nothing is deleted: history and catalogue survive suspension.
    assert await _order_count(db, everyone) == orders_before == 100
    assert (await db.execute(select(func.count()).select_from(MenuItem).where(MenuItem.business_id.in_(everyone)))).scalar() == items_before == 60
    assert (await db.execute(select(func.count()).select_from(BusinessUser).where(BusinessUser.business_id.in_(everyone)))).scalar() == 60


async def test_reconcile_moving_back_in_time_restores_access_only_for_its_own_suspension(db, bag, clock):
    """Suspended (ENDED) vendors come back when the window reopens (a calendar/prep change), by reason."""
    ev = await _event(db, bag)
    vendors = (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[
        _row(bag, starts=D[0], ends=D[4]) for _ in range(6)]), ADMIN, db))["vendors"]
    ids = _ids([{**v, "cohort": 0} for v in vendors])
    await _reconcile(db, clock, T_BEFORE, ids)
    await _assert_biz(db, vendors, active=False, reason=EVENT_SUSPENSION_REASON)
    await _reconcile(db, clock, T_PREP, ids)
    await _assert_biz(db, vendors, active=True, reason=None)
    await _reconcile(db, clock, T_AFTER, ids)
    await _assert_biz(db, vendors, active=False, reason=EVENT_SUSPENSION_REASON)


async def test_admin_suspension_is_not_lifted_by_reconcile_at_any_point_of_the_lifecycle(db, bag, clock):
    ev = await _event(db, bag)
    vendors = (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[
        _row(bag, starts=D[0], ends=D[4]) for _ in range(8)]), ADMIN, db))["vendors"]
    subs = await _sub_ids(db, ev["id"])
    for v in vendors:
        await _pay(db, ev, subs[v["vendor_id"]])
    ids = [v["business_id"] for v in vendors]
    held, others = vendors[:3], vendors[3:]
    await _reconcile(db, clock, T_PREP, ids)
    for v in held:
        out = await suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="complaint"), ADMIN, db)
        assert out["suspended_reason"] == EVENT_ADMIN_SUSPENSION_REASON
    for at in (T_PREP, T_DAY1, T_DAY3, T_BEFORE, T_DAY1):  # forward, backward, forward: never lifted
        await _reconcile(db, clock, at, ids)
        await _assert_biz(db, held, active=False, reason=EVENT_ADMIN_SUSPENSION_REASON)
        if at != T_BEFORE:
            await _assert_biz(db, others, active=True, reason=None)
    await _reconcile(db, clock, T_AFTER, ids)
    # The administrator's reason survives the end of the event (it is not overwritten by SUBSCRIPTION_ENDED).
    await _assert_biz(db, held, active=False, reason=EVENT_ADMIN_SUSPENSION_REASON)
    await _assert_biz(db, others, active=False, reason=EVENT_SUSPENSION_REASON)
    # Reinstating one while the event is over: the lift is immediately re-suspended by the event end.
    out = await reinstate_vendor(ev["id"], held[0]["vendor_id"], ADMIN, db)
    assert (out["business_active"], out["suspended_reason"]) == (False, EVENT_SUSPENSION_REASON)
    # Reinstating one during the event: stays on, and reconcile keeps it on.
    clock.now = T_DAY1
    out = await reinstate_vendor(ev["id"], held[1]["vendor_id"], ADMIN, db)
    assert (out["business_active"], out["suspended_reason"]) == (True, None)
    await _reconcile(db, clock, T_DAY1, ids)
    await _assert_biz(db, [held[1]], active=True, reason=None)


# ==============================================================================
# 3. Tenant isolation at scale
# ==============================================================================

async def test_tenant_isolation_across_60_vendors_sharing_the_same_sku_and_barcode(db, bag, clock):
    """
    Every vendor owns a menu item, a variant (the SAME sku and barcode in all 60 businesses, plus a unique
    barcode), a paid sale and an event subscription. Vendor i then attacks vendor i+1 (a full ring) through
    every read/write route; every attempt is 404 and nothing of the victim changes.
    """
    ev = await _event(db, bag)
    clock.now = T_DAY1
    rows = [_row(bag, "FOOD" if i % 2 == 0 else "CLOTHING", starts=D[0], ends=D[4]) for i in range(60)]
    vendors = (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db))["vendors"]
    subs = await _sub_ids(db, ev["id"])
    for v in vendors:
        await _pay(db, ev, subs[v["vendor_id"]])
        v["sub_id"] = subs[v["vendor_id"]]
    for i, v in enumerate(vendors):
        owner = _user(v["business_id"])
        item = await _shop(db, owner, price=5000, name=f"Item {i}")
        var = await create_variant(item.id, VariantCreate(sku="SHARED-SKU", barcode="SHARED-BC", attributes={"size": "M"},
                                                          opening_stock=5), owner, db)
        uniq = await create_variant(item.id, VariantCreate(sku=f"U-{i}", barcode=f"UNIQ-BC-{i}",
                                                           attributes={"size": "L"}, opening_stock=5), owner, db)
        order = await create_pos_order(POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, variant_id=var.id, quantity=1)],
            payment_method="CASH", cash_tendered_cents=5000), owner, db)
        v.update(item=item.id, var=var.id, uniq=uniq.id, order=order.id, owner=owner)
    ids = [v["business_id"] for v in vendors]
    assert len({v["item"] for v in vendors}) == 60 and len({v["var"] for v in vendors}) == 60

    async def state():
        items = (await db.execute(select(MenuItem.id, MenuItem.name, MenuItem.price_cents, MenuItem.is_active, MenuItem.is_deleted)
                                  .where(MenuItem.business_id.in_(ids)).order_by(MenuItem.id))).all()
        vars_ = (await db.execute(select(ProductVariant.id, ProductVariant.sku, ProductVariant.barcode, ProductVariant.price_cents,
                                         ProductVariant.is_active).where(ProductVariant.business_id.in_(ids))
                                  .order_by(ProductVariant.id))).all()
        ords = (await db.execute(select(Order.id, Order.status, Order.payment_status, Order.total_cents)
                                 .where(Order.business_id.in_(ids)).order_by(Order.id))).all()
        sub = (await db.execute(select(EventVendorSubscription.id, EventVendorSubscription.status,
                                       EventVendorSubscription.payment_claim_reference)
                                .where(EventVendorSubscription.business_id.in_(ids)).order_by(EventVendorSubscription.id))).all()
        events = (await db.execute(select(OrderEvent.id).where(OrderEvent.business_id.in_(ids)))).all()
        return [list(map(tuple, x)) for x in (items, vars_, ords, sub, events)]

    before = await state()
    assert [len(x) for x in before] == [60, 120, 60, 60, 60]
    for i, a in enumerate(vendors):
        b = vendors[(i + 1) % 60]
        u = a["owner"]
        attempts = [
            list_variants(b["item"], False, u, db),
            update_variant(b["var"], VariantUpdate(price_cents=1), u, db),
            update_variant(b["uniq"], VariantUpdate(is_active=False), u, db),
            deactivate_variant(b["var"], u, db),
            create_variant(b["item"], VariantCreate(sku="EVIL", attributes={"a": "b"}), u, db),
            create_variant_matrix(b["item"], MatrixCreate(sku_prefix="EV", sizes=["S"], opening_stock=1), u, db),
            update_item(b["item"], ItemUpdate(name="pwned", price_cents=1), u, db),
            soft_delete_item(b["item"], u, db),
            get_order(b["order"], u, db),
            update_order_status(b["order"], StatusUpdateRequest(status="IN_PROGRESS"), u, db),
            update_payment_status(b["order"], PaymentUpdateRequest(payment_status="CASH_ON_COLLECTION"), u, db),
            refund_pos_order(b["order"], POSRefundRequest(reason="steal"), u, db),
            ven.claim_payment(b["sub_id"], ven.PaymentClaim(reference=f"EVIL-{i}"), u, db),
            ven.cancel_subscription(b["sub_id"], u, db),
            pos_lookup(f"UNIQ-BC-{(i + 1) % 60}", None, u, db),           # victim's unique barcode
            _checkout(db, u, [_sell(b["var"], 1, b["item"])]),             # sell the victim's variant/item
            _checkout(db, u, [_sell(b["var"], 1, a["item"])]),             # victim variant on my own item
            _checkout(db, u, [_sell(a["var"], 1, b["item"])]),             # my variant on the victim's item
        ]
        for n, call in enumerate(attempts):
            await _refused(call, status=404)
            await db.rollback()
        # The shared barcode resolves to MY variant, never the neighbour's.
        hit = await pos_lookup("SHARED-BC", None, u, db)
        assert (hit.variant.id, hit.menu_item_id) == (a["var"], a["item"])
        # Positive controls: my own lists contain only my own rows.
        assert [x.id for x in await list_items(None, True, u, db)] == [a["item"]]
        assert {x.id for x in await list_variants(a["item"], False, u, db)} == {a["var"], a["uniq"]}
        mine = await list_orders(status=None, live=False, cursor=None, limit=100, user=u, db=db)
        assert [getattr(o, "id", None) or o["id"] for o in mine.data] == [a["order"]],             "list_orders leaked another tenant's order"
        mine_subs = (await ven.list_event_subscriptions(u, db))["events"]
        assert [s["id"] for e in mine_subs for s in e["subscriptions"]] == [a["sub_id"]]
    assert await state() == before, "a cross-tenant attempt changed the victim's data"


# ==============================================================================
# 4. Food and clothing workflows end to end
# ==============================================================================

async def _two_vendors(db, bag, clock):
    ev = await _event(db, bag)
    clock.now = T_DAY1
    food, cloth = (await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[
        _row(bag, "FOOD", starts=D[0], ends=D[4]), _row(bag, "CLOTHING", starts=D[0], ends=D[4])]), ADMIN, db))["vendors"]
    subs = await _sub_ids(db, ev["id"])
    for v in (food, cloth):
        await _pay(db, ev, subs[v["vendor_id"]])
    await _reconcile(db, clock, T_DAY1, [food["business_id"], cloth["business_id"]])
    return ev, food, cloth


async def test_food_vendor_menu_to_pos_sale_with_consent_to_kitchen_to_completed(db, bag, clock):
    ev, food, _ = await _two_vendors(db, bag, clock)
    industry = (await db.execute(select(Business.industry).where(Business.id == food["business_id"]))).scalar_one()
    assert industry == "EVENT_FOOD"
    owner, staff = _user(food["business_id"]), _user(food["business_id"], "STAFF")
    item = await _shop(db, owner, requires_prep=True, price=4500, name="Souk Burger")
    assert [i.name for i in await list_items(None, False, owner, db)] == ["Souk Burger"]

    with patch("backend.app.bot.whatsapp_sender.send_notification_message", new=AsyncMock(return_value="wamid")) as send:
        async def sell(consent):
            kw = dict(customer_phone="0821112222", notify_when_ready=True) if consent else {}
            return await create_pos_order(POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=2)], payment_method="CASH",
                cash_tendered_cents=9000, **kw), owner, db)

        with_consent, without = await sell(True), await sell(False)
        flags = dict((await db.execute(select(Order.id, Order.notify_when_ready).where(
            Order.id.in_([with_consent.id, without.id])))).all())
        assert flags == {with_consent.id: True, without.id: False}
        for o in (with_consent, without):
            assert (o.status, o.payment_status, o.total_cents) == ("ACCEPTED", "PAID", 9000)  # kitchen item -> ACCEPTED

        for order in (with_consent, without):
            for target in ("IN_PROGRESS", "READY", "COMPLETED"):
                out = await update_order_status(order.id, StatusUpdateRequest(status=target), staff, db)
                assert out.status == target
            # a terminal order cannot move again
            assert await _refused(update_order_status(order.id, StatusUpdateRequest(status="READY"), staff, db),
                                  status=422) == "INVALID_STATUS_TRANSITION"
            await db.rollback()
        trail = (await db.execute(select(OrderEvent.order_id, OrderEvent.new_status).where(
            OrderEvent.order_id.in_([with_consent.id, without.id])).order_by(OrderEvent.created_at))).all()
        for oid in (with_consent.id, without.id):
            assert [s for o, s in trail if o == oid] == ["ACCEPTED", "IN_PROGRESS", "READY", "COMPLETED"]
        # No message for the order without consent. For the consented one, an event vendor has no
        # whatsapp_phone_number_id (WhatsApp is off for event vendors, security review H1), so the
        # notification path has no sender identity either: nothing is sent, and nothing real could be.
        assert send.await_count == 0
        wa = (await db.execute(select(Business.whatsapp_phone_number_id, Business.is_whatsapp_enabled)
                               .where(Business.id == food["business_id"]))).one()
        assert tuple(wa) == (None, False)

    final = (await db.execute(select(Order.status, Order.payment_status).where(Order.business_id == food["business_id"]))).all()
    assert Counter(final) == {("COMPLETED", "PAID"): 2}


async def test_clothing_vendor_variants_barcode_sale_line_return_and_exchange(db, bag, clock):
    ev, _, cloth = await _two_vendors(db, bag, clock)
    industry = (await db.execute(select(Business.industry).where(Business.id == cloth["business_id"]))).scalar_one()
    assert industry == "EVENT_RETAIL"
    biz = SimpleNamespace(id=cloth["business_id"])  # plain id holder: ORM rows expire on every rollback below
    owner = _user(cloth["business_id"])
    item = await _shop(db, owner, requires_prep=False, price=10_000, name="Linen Shirt")
    out = await create_variant_matrix(item.id, MatrixCreate(sku_prefix="LS", sizes=["S", "M", "L"], colours=["Black"],
                                                            opening_stock=5, cost_cents=4000), owner, db)
    vs = {v.sku: v for v in out}
    assert sorted(vs) == ["LS-L-BLACK", "LS-M-BLACK", "LS-S-BLACK"]
    m, l = vs["LS-M-BLACK"], vs["LS-L-BLACK"]
    await update_variant(m.id, VariantUpdate(barcode="6001000000017"), owner, db)
    await update_variant(l.id, VariantUpdate(barcode="6001000000024", price_cents=11_000), owner, db)

    hit = await pos_lookup("6001000000017", None, owner, db)
    assert (hit.variant.id, hit.menu_item_id, hit.base_price_cents) == (m.id, item.id, 10_000)
    sale = await _checkout(db, owner, [_sell(hit.variant.id, 1, hit.menu_item_id)])
    assert (sale.status, sale.payment_status, sale.total_cents) == ("COMPLETED", "PAID", 10_000)  # no prep -> grab-and-go
    assert await _stock(db, biz, m, l) == [4, 5]

    # Line return with restock: money back for that line, stock back.
    line = sale.items[0]
    refunded = await refund_pos_order(
        sale.id, POSRefundRequest(reason="wrong size", restore_stock=True,
                                  return_items=[POSReturnLine(order_item_id=line.id, quantity=1)]), owner, db)
    assert (refunded.refund_amount_cents, refunded.payment_status) == (10_000, "REFUNDED")
    assert await _stock(db, biz, m, l) == [5, 5]
    assert await _refused(refund_pos_order(  # returning the same unit twice is refused
        sale.id, POSRefundRequest(reason="again", restore_stock=True,
                                  return_items=[POSReturnLine(order_item_id=line.id, quantity=1)]), owner, db)) is not None
    await db.rollback()
    assert await _stock(db, biz, m, l) == [5, 5]

    # Exchange: the replacement (L, price override) is linked to the refunded order.
    scanned = await pos_lookup("6001000000024", None, owner, db)
    exch = await _checkout(db, owner, [_sell(scanned.variant.id, 1, scanned.menu_item_id)], exchange_of_order_id=sale.id)
    assert (exch.exchange_of_order_id, exch.total_cents, exch.status) == (sale.id, 11_000, "COMPLETED")
    assert await _stock(db, biz, m, l) == [5, 4]
    # The exchange link cannot be reused on an order that is not refunded.
    assert await _refused(_checkout(db, owner, [_sell(m.id, 1, item.id)], exchange_of_order_id=exch.id)) \
        == "EXCHANGE_ORIGINAL_NOT_REFUNDED"
    await db.rollback()
    returned = (await db.execute(select(func.sum(Order.refund_amount_cents)).where(Order.business_id == cloth["business_id"]))).scalar()
    assert returned == 10_000


async def test_vendor_types_do_not_cross_over(db, bag, clock):
    """A clothing vendor's variant cannot be sold by the food vendor, and vice versa (same event, both paid)."""
    ev, food, cloth = await _two_vendors(db, bag, clock)
    fo, co = _user(food["business_id"]), _user(cloth["business_id"])
    fitem = await _shop(db, fo, requires_prep=True, name="Food Thing")
    citem = await _shop(db, co, requires_prep=False, name="Cloth Thing")
    cvar = await create_variant(citem.id, VariantCreate(sku="X-1", barcode="BC-X", attributes={"size": "M"}, opening_stock=3), co, db)
    assert await _refused(_checkout(db, fo, [_sell(cvar.id, 1, citem.id)]), status=404)
    await db.rollback()
    assert await _refused(_checkout(db, fo, [_sell(cvar.id, 1, fitem.id)]), status=404)
    await db.rollback()
    assert await _refused(pos_lookup("BC-X", None, fo, db), status=404)
    assert await _order_count(db, [food["business_id"]]) == 0
