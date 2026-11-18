"""
Real-Postgres tests for souk administration (admin_api/app/api/v1/routes_events.py)
and the shared provisioning service. Route functions are called directly with
a real session, the same pattern as the other workflow integration tests.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, func, select

from admin_api.app.api.v1.routes_events import (
    BulkVendorCsvRequest,
    BulkVendorRequest,
    CalendarUpdate,
    EventCreate,
    EventDayIn,
    MarkPaid,
    VendorRow,
    bulk_onboard_vendors,
    bulk_onboard_vendors_csv,
    create_event,
    list_vendors,
    mark_subscription_paid,
    replace_calendar,
)
from admin_api.app.services.provisioning import provision_business
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventVendor
from shared.models.user import BusinessUser

pytestmark = pytest.mark.asyncio

# Relative to today: these routes run the access reconcile on the real clock, so a
# fixed calendar date would make the tests fail once it passed (review L7). A week
# ahead sits inside the 14-day setup window.
START = date.today() + timedelta(days=7)
DAYS = [START + timedelta(days=i) for i in range(19)]
ADMIN = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


@pytest.fixture
async def cleanup(db):
    """Collects event ids and owner emails; deletes them (and their businesses) afterwards."""
    bag = {"events": [], "emails": []}
    yield bag
    await db.rollback()
    if bag["emails"]:
        biz_ids = (await db.execute(
            select(BusinessUser.business_id).where(BusinessUser.email.in_(bag["emails"]))
        )).scalars().all()
        if biz_ids:
            await db.execute(delete(Business).where(Business.id.in_(biz_ids)))
    if bag["events"]:
        await db.execute(delete(Event).where(Event.id.in_(bag["events"])))
    await db.commit()


async def _souk(db, bag) -> dict:
    ev = await create_event(EventCreate(name=f"Souk {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=d) for d in DAYS]), ADMIN, db)
    bag["events"].append(ev["id"])
    return ev


def _row(bag, category="FOOD", stall=None, full=True, **kw) -> VendorRow:
    email = f"vendor-{uuid.uuid4().hex[:8]}@example.com"
    bag["emails"].append(email)
    base = dict(category=category, stall_code=stall, business_name=f"Stall {uuid.uuid4().hex[:5]}",
                owner_email=email, owner_full_name="Vendor Owner")
    if full:
        base.update(starts_on=DAYS[0], ends_on=DAYS[-1])
    base.update(kw)
    return VendorRow(**base)


async def test_event_defaults_to_owner_prices_and_counts_19_billable_days(db, cleanup):
    ev = await _souk(db, cleanup)
    assert (ev["food_day_rate_cents"], ev["clothing_day_rate_cents"]) == (10_000, 5_000)
    assert ev["billable_day_count"] == 19 and ev["status"] == "DRAFT"


async def test_dry_run_quotes_and_creates_nothing(db, cleanup):
    ev = await _souk(db, cleanup)
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(
        vendors=[_row(cleanup, "FOOD"), _row(cleanup, "CLOTHING")], dry_run=True), ADMIN, db)
    assert [v["amount_cents"] for v in res["vendors"]] == [190_000, 95_000]
    count = (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar()
    assert count == 0


async def test_bulk_onboarding_creates_isolated_event_businesses(db, cleanup):
    ev = await _souk(db, cleanup)
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(
        vendors=[_row(cleanup, "FOOD", stall="F1"), _row(cleanup, "CLOTHING", stall="C1")]), ADMIN, db)
    assert len(res["vendors"]) == 2
    for v in res["vendors"]:
        assert v["owner_temporary_password"]  # shown once, in this response only
        biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
        assert biz.plan == EVENT_PLAN and biz.pos_enabled and biz.inventory_enabled
        assert biz.industry in ("EVENT_FOOD", "EVENT_RETAIL")
        # Never an inherited external identity.
        assert biz.whatsapp_phone_number_id is None and biz.whatsapp_business_account_id is None
        assert biz.payment_api_key is None and biz.payment_webhook_secret is None
        owner = (await db.execute(select(BusinessUser).where(BusinessUser.business_id == biz.id))).scalar_one()
        assert owner.role == "OWNER" and owner.must_change_password is True
    vendors = await list_vendors(ev["id"], ADMIN, db)
    assert sorted(s["amount_cents"] for v in vendors for s in v["subscriptions"]) == [95_000, 190_000]
    assert all(s["status"] == "UNPAID" for v in vendors for s in v["subscriptions"])


async def test_one_bad_row_creates_nothing(db, cleanup):
    ev = await _souk(db, cleanup)
    with pytest.raises(AppError) as exc:
        await bulk_onboard_vendors(ev["id"], BulkVendorRequest(
            vendors=[_row(cleanup, stall="A1"), _row(cleanup, stall="A1")]), ADMIN, db)
    assert exc.value.code == "BULK_VALIDATION_FAILED"
    assert {r["row"] for r in exc.value.details["rows"]} == {1, 2}
    await db.rollback()
    count = (await db.execute(select(func.count()).select_from(EventVendor).where(EventVendor.event_id == ev["id"]))).scalar()
    assert count == 0


async def test_a_permanent_tenant_cannot_be_put_on_event_billing(db, cleanup, test_business):
    ev = await _souk(db, cleanup)
    with pytest.raises(AppError) as exc:
        await bulk_onboard_vendors(ev["id"], BulkVendorRequest(
            vendors=[VendorRow(category="FOOD", existing_business_id=test_business.id)]), ADMIN, db)
    assert "not an event-plan vendor" in exc.value.details["rows"][0]["errors"][0]


async def test_payment_activates_immediately_and_is_idempotent(db, cleanup):
    ev = await _souk(db, cleanup)
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[_row(cleanup)]), ADMIN, db)
    vendor = (await list_vendors(ev["id"], ADMIN, db))[0]
    sub_id = vendor["subscriptions"][0]["id"]
    assert (await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-001"), ADMIN, db))["already_paid"] is False
    again = await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-001"), ADMIN, db)
    assert again["already_paid"] is True
    with pytest.raises(AppError) as exc:
        await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-999"), ADMIN, db)
    assert exc.value.code == "ALREADY_PAID"
    await db.rollback()
    vendor = (await list_vendors(ev["id"], ADMIN, db))[0]
    assert vendor["status"] == "ACTIVE" and vendor["business_active"] is True
    assert res["vendors"][0]["business_id"] == vendor["business_id"]


async def test_a_paid_day_cannot_be_removed_from_the_calendar(db, cleanup):
    ev = await _souk(db, cleanup)
    await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[_row(cleanup)]), ADMIN, db)
    sub_id = (await list_vendors(ev["id"], ADMIN, db))[0]["subscriptions"][0]["id"]
    await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-002"), ADMIN, db)
    with pytest.raises(AppError) as exc:
        await replace_calendar(ev["id"], CalendarUpdate(days=[EventDayIn(day=d) for d in DAYS[:-1]]), ADMIN, db)
    assert exc.value.code == "PAID_DAY_REMOVED"


async def test_csv_import(db, cleanup):
    ev = await _souk(db, cleanup)
    e1, e2 = (f"csv-{uuid.uuid4().hex[:8]}@example.com" for _ in range(2))
    cleanup["emails"] += [e1, e2]
    csv_text = (
        "category,stall_code,business_name,owner_email,starts_on,ends_on\n"
        f"food,F9,Bunny Chow Co,{e1},{DAYS[0]},{DAYS[2]}\n"
        f"CLOTHING,C9,Kaftan House,{e2},,\n"
    )
    res = await bulk_onboard_vendors_csv(ev["id"], BulkVendorCsvRequest(csv_text=csv_text), ADMIN, db)
    assert [v["amount_cents"] for v in res["vendors"]] == [30_000, None]


async def test_provisioning_refuses_external_identities(db):
    with pytest.raises(AppError) as exc:
        await provision_business(db, fields={"name": "X", "whatsapp_phone_number_id": "123"})
    assert exc.value.code == "FIELD_NOT_ALLOWED"


# ── Review fixes, 2026-11-18 ──────────────────────────────────────────────────

async def test_h1_single_business_create_still_works_with_and_without_a_whatsapp_number(db):
    """H1 regression: the admin create form always sends whatsapp_phone_number_id (often None)."""
    unique = uuid.uuid4().hex[:8]
    created = []
    try:
        plain = await provision_business(db, fields={"name": f"Plain {unique}", "whatsapp_phone_number_id": None})
        created.append(plain.business.id)
        with_number = await provision_business(
            db, fields={"name": f"Numbered {unique}", "whatsapp_phone_number_id": f"99{unique}"},
            allow_external_identity=True,
        )
        created.append(with_number.business.id)
        assert with_number.business.whatsapp_phone_number_id == f"99{unique}"
        with pytest.raises(AppError):
            await provision_business(db, fields={"name": f"Bulk {unique}", "whatsapp_phone_number_id": "123"})
    finally:
        await db.rollback()


async def test_stale_quote_cannot_be_marked_paid(db, cleanup):
    ev = await _souk(db, cleanup)
    await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[_row(cleanup)]), ADMIN, db)
    sub_id = (await list_vendors(ev["id"], ADMIN, db))[0]["subscriptions"][0]["id"]
    # Calendar changes before payment: two more billable days.
    extra = [DAYS[-1] + timedelta(days=i) for i in (1, 2)]
    await replace_calendar(ev["id"], CalendarUpdate(days=[EventDayIn(day=d) for d in DAYS + extra]), ADMIN, db)
    # The quote covered DAYS[0]..DAYS[-1]; still 19 days in that range, so it remains valid...
    await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-OK"), ADMIN, db)
    # ...but a quote whose range changes size is refused.
    res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=[_row(cleanup, full=False)]), ADMIN, db)
    from admin_api.app.api.v1.routes_events import SubscriptionCreate, create_subscription
    q = await create_subscription(ev["id"], res["vendors"][0]["vendor_id"],
                                  SubscriptionCreate(starts_on=extra[0], ends_on=extra[1]), ADMIN, db)
    await replace_calendar(ev["id"], CalendarUpdate(days=[EventDayIn(day=d) for d in DAYS + extra[:1]]), ADMIN, db)
    with pytest.raises(AppError) as exc:
        await mark_subscription_paid(ev["id"], q["id"], MarkPaid(payment_reference="EFT-STALE"), ADMIN, db)
    assert exc.value.code == "QUOTE_STALE"


async def test_a_paid_range_cannot_gain_an_unpaid_billable_day(db, cleanup):
    ev = await create_event(EventCreate(name=f"Gap {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=DAYS[0]), EventDayIn(day=DAYS[1], is_billable=False),
                                              EventDayIn(day=DAYS[2])]), ADMIN, db)
    cleanup["events"].append(ev["id"])
    await bulk_onboard_vendors(ev["id"], BulkVendorRequest(
        vendors=[_row(cleanup, starts_on=DAYS[0], ends_on=DAYS[2])]), ADMIN, db)
    sub_id = (await list_vendors(ev["id"], ADMIN, db))[0]["subscriptions"][0]["id"]
    await mark_subscription_paid(ev["id"], sub_id, MarkPaid(payment_reference="EFT-GAP"), ADMIN, db)
    with pytest.raises(AppError) as exc:
        await replace_calendar(ev["id"], CalendarUpdate(days=[EventDayIn(day=d) for d in DAYS[:3]]), ADMIN, db)
    assert exc.value.code == "PAID_RANGE_GAINS_DAY"


async def test_csv_over_the_cap_is_refused_before_any_work(db, cleanup):
    from admin_api.app.api.v1.routes_events import MAX_BULK_VENDORS
    ev = await _souk(db, cleanup)
    lines = ["category,business_name,owner_email"] + [
        f"FOOD,Stall {i},cap-{i}-{uuid.uuid4().hex[:4]}@example.com" for i in range(MAX_BULK_VENDORS + 1)
    ]
    csv_text = chr(10).join(lines)
    with pytest.raises(AppError) as exc:
        await bulk_onboard_vendors_csv(ev["id"], BulkVendorCsvRequest(csv_text=csv_text), ADMIN, db)
    assert exc.value.code == "CSV_TOO_LARGE"


async def test_csv_without_category_column_is_refused(db, cleanup):
    ev = await _souk(db, cleanup)
    csv_text = chr(10).join(["business_name,owner_email", "X,x@example.com", ""])
    with pytest.raises(AppError) as exc:
        await bulk_onboard_vendors_csv(ev["id"], BulkVendorCsvRequest(csv_text=csv_text), ADMIN, db)
    assert exc.value.code == "CSV_COLUMNS"
