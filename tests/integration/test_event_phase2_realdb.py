"""
Real-Postgres tests for souk Phase 2: vendor self-registration, public event
page, registration-code handling, and vendor self-service subscriptions.

Route functions are called directly with a real session (same pattern as
test_event_phase1_realdb.py). Public routes are called through ``__wrapped__``
so slowapi's Redis-backed counter is not engaged; the limiter configuration
itself is asserted statically (as test_public_receipt_realdb.py does).

The calendar is relative to today (Africa/Johannesburg) because the routes run
the access reconcile on the real clock.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from admin_api.app.api.v1.routes_events import (
    EventCreate,
    EventDayIn,
    EventStatusUpdate,
    MarkPaid,
    RegistrationUpdate,
    SubscriptionCreate,
    create_event,
    create_subscription,
    get_event,
    list_vendors,
    mark_subscription_paid,
    set_event_status,
    update_registration,
)
from backend.app.api.v1 import routes_event_public as pub
from backend.app.api.v1 import routes_event_vendor as ven
from backend.app.api.v1 import routes_email_verification as ver
from backend.app.api.v1.routes_business import get_event_access
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.core.ratelimit import limiter
from backend.app.core.security import verify_password
from backend.app.events.trading import assert_trading_allowed
from backend.app.services import email_verification as ev_email
from backend.app.services.auth_service import AuthService
from shared.enums import EVENT_PLAN
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.event import Event, EventVendor, EventVendorSubscription
from shared.models.user import BusinessUser

pytestmark = pytest.mark.asyncio

SAST = ZoneInfo("Africa/Johannesburg")
TODAY = datetime.now(SAST).date()
DAYS = [TODAY + timedelta(days=i) for i in range(6)]  # today .. today+5
ADMIN = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")
PASSWORD = "Vendor-chosen-pw-1"

_INFO = pub.get_public_event.__wrapped__
_REGISTER = pub.register_vendor.__wrapped__


def _owner(business_id) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


@pytest.fixture
async def bag(db):
    bag = {"events": [], "emails": []}
    yield bag
    await db.rollback()
    if bag["emails"]:
        biz_ids = (await db.execute(
            select(BusinessUser.business_id).where(BusinessUser.email.in_(bag["emails"])))).scalars().all()
        if biz_ids:
            await db.execute(delete(AuditEvent).where(AuditEvent.business_id.in_(biz_ids)))
            await db.execute(delete(Business).where(Business.id.in_(biz_ids)))
    if bag["events"]:
        await db.execute(delete(AuditEvent).where(AuditEvent.target_id.in_(bag["events"])))
        await db.execute(delete(Event).where(Event.id.in_(bag["events"])))
    await db.commit()


async def _open_event(db, bag, days=DAYS, status="OPEN", registration=True) -> dict:
    """An event with registration open; returns {id, slug, code}."""
    ev = await create_event(EventCreate(name=f"P2 Souk {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=d) for d in days]), ADMIN, db)
    bag["events"].append(ev["id"])
    code = None
    if registration:
        code = (await update_registration(ev["id"], RegistrationUpdate(open=True, rotate_code=True), ADMIN, db))[
            "registration_code"]
    if status != "DRAFT":
        await set_event_status(ev["id"], EventStatusUpdate(status=status), ADMIN, db)
    return {"id": ev["id"], "slug": ev["slug"], "code": code}


def _body(bag, code, category="FOOD", **kw) -> pub.RegisterRequest:
    email = f"p2-{uuid.uuid4().hex[:8]}@example.com"
    bag["emails"].append(email)
    base = dict(registration_code=code, business_name=f"P2 Stall {uuid.uuid4().hex[:5]}", category=category,
                owner_full_name="Vendor Owner", owner_email=email, password=PASSWORD, accept_terms=True)
    base.update(kw)
    return pub.RegisterRequest(**base)


async def _register(db, bag, ev, category="FOOD", verify=True, **kw) -> dict:
    """
    Self-register a vendor; returns the response plus the owner's AuthUser and email.

    Since email verification (migration 0070) the register response no longer
    carries the business code, so the business is found by the owner's email.
    ``verify=True`` (the default) completes verification through the real
    POST /public/auth/verify-email route with a freshly issued token, so tests
    about subscriptions and trading keep exercising a legitimate, verified
    vendor. Pass verify=False to keep the account unverified.
    """
    body = _body(bag, ev["code"], category, **kw)
    res = await _REGISTER(ev["slug"], body, None, db)
    email = str(body.owner_email).lower()
    user = (await db.execute(select(BusinessUser).where(BusinessUser.email == email))).scalar_one()
    biz = (await db.execute(select(Business).where(Business.id == user.business_id))).scalar_one()
    if verify:
        raw = await ev_email.issue_token(db, user.id)
        await db.commit()
        await ver.verify_email.__wrapped__(ver.VerifyEmailRequest(token=raw), None, db)
    return {**res, "response": res, "business_code": biz.business_code, "business_id": biz.id,
            "user": _owner(biz.id), "email": str(body.owner_email), "owner_id": user.id}


async def _refused(coro) -> AppError:
    with pytest.raises(AppError) as exc:
        await coro
    return exc.value


# ── Public event page ─────────────────────────────────────────────────────────

async def test_public_event_page_exposes_only_public_facts_and_no_vendor_data(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    info = await _INFO(ev["slug"], None, db)
    assert set(info) == {"name", "slug", "venue", "timezone", "status", "prep_days", "days",
                         "food_day_rate_cents", "clothing_day_rate_cents", "registration_open"}
    assert info["registration_open"] is True and len(info["days"]) == 6
    blob = repr(info)
    assert v["business_code"] not in blob and v["email"] not in blob and ev["code"] not in blob


async def test_public_event_404_is_identical_for_draft_cancelled_and_unknown(db, bag):
    draft = await _open_event(db, bag, status="DRAFT")
    cancelled = await _open_event(db, bag, status="CANCELLED")
    errs = [await _refused(_INFO(slug, None, db)) for slug in (draft["slug"], cancelled["slug"], "no-such-event")]
    assert {(e.code, e.message, e.status_code) for e in errs} == {("EVENT_NOT_FOUND", "Event not found", 404)}


async def test_registration_open_flag_is_false_when_closed(db, bag):
    ev = await _open_event(db, bag)
    await update_registration(ev["id"], RegistrationUpdate(open=False), ADMIN, db)
    assert (await _INFO(ev["slug"], None, db))["registration_open"] is False


# ── Registration code ─────────────────────────────────────────────────────────

async def test_code_rotation_returns_plaintext_once_and_stores_only_a_hash(db, bag):
    ev = await _open_event(db, bag)
    code = ev["code"]
    assert len(code.replace("-", "")) == 8 and not set(code.replace("-", "")) & set("01OIL")
    row = (await db.execute(select(Event).where(Event.id == ev["id"]))).scalar_one()
    assert row.registration_code_hash and code not in row.registration_code_hash
    assert row.registration_code_hash.startswith("$2")
    assert verify_password(code.replace("-", ""), row.registration_code_hash)
    detail = await get_event(ev["id"], ADMIN, db)
    assert detail["registration_open"] is True and detail["has_registration_code"] is True
    assert code not in repr(detail) and "registration_code" not in detail
    audits = (await db.execute(select(AuditEvent.diff_json).where(AuditEvent.target_id == ev["id"]))).scalars().all()
    assert audits and code not in repr(audits) and code.replace("-", "") not in repr(audits)
    # A second call without rotation returns no code and keeps the old one valid.
    again = await update_registration(ev["id"], RegistrationUpdate(open=True), ADMIN, db)
    assert again["registration_code"] is None and again["has_registration_code"] is True
    await _register(db, bag, ev)
    # Rotation invalidates the old code.
    new = (await update_registration(ev["id"], RegistrationUpdate(open=True, rotate_code=True), ADMIN, db))["registration_code"]
    assert new != code
    assert (await _refused(_REGISTER(ev["slug"], _body(bag, code), None, db))).code == "REGISTRATION_REJECTED"
    await _register(db, bag, {**ev, "code": new})


async def test_opening_an_event_without_a_code_generates_one(db, bag):
    ev = await _open_event(db, bag, registration=False)
    out = await update_registration(ev["id"], RegistrationUpdate(open=True), ADMIN, db)
    assert out["registration_code"] and out["has_registration_code"] is True


async def test_wrong_code_closed_registration_unknown_slug_and_unopened_event_are_indistinguishable(db, bag):
    open_ev = await _open_event(db, bag)
    closed = await _open_event(db, bag)
    await update_registration(closed["id"], RegistrationUpdate(open=False), ADMIN, db)
    draft = await _open_event(db, bag, status="DRAFT")
    cases = [
        await _refused(_REGISTER(open_ev["slug"], _body(bag, "WRONG-CD"), None, db)),             # wrong code
        await _refused(_REGISTER(closed["slug"], _body(bag, closed["code"]), None, db)),           # closed, right code
        await _refused(_REGISTER(draft["slug"], _body(bag, draft["code"]), None, db)),             # not OPEN, right code
        await _refused(_REGISTER("no-such-event", _body(bag, open_ev["code"]), None, db)),         # unknown slug
    ]
    assert len({(e.code, e.message, e.status_code) for e in cases}) == 1
    assert cases[0].code == "REGISTRATION_REJECTED"
    n = (await db.execute(select(func.count()).select_from(EventVendor).where(
        EventVendor.event_id.in_([open_ev["id"], closed["id"], draft["id"]])))).scalar()
    assert n == 0


async def test_code_is_case_and_hyphen_insensitive(db, bag):
    ev = await _open_event(db, bag)
    await _register(db, bag, {**ev, "code": ev["code"].lower().replace("-", " ")})


# ── Registration ──────────────────────────────────────────────────────────────

async def test_registration_creates_an_isolated_event_business_with_the_vendors_own_password(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev, "CLOTHING")
    # Changed with email verification (owner request): the response says
    # "check your email" and carries no account data (no business code).
    assert v["response"]["next"] == "check_email" and "business_code" not in v["response"]
    assert not {"access_token", "refresh_token", "token"} & set(v["response"])
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    assert biz.plan == EVENT_PLAN and biz.industry == "EVENT_RETAIL" and biz.pos_enabled and biz.inventory_enabled
    assert biz.whatsapp_phone_number_id is None and biz.whatsapp_business_account_id is None
    assert biz.payment_api_key is None and biz.payment_webhook_secret is None
    owner = (await db.execute(select(BusinessUser).where(BusinessUser.business_id == biz.id))).scalar_one()
    assert owner.role == "OWNER" and owner.must_change_password is False and owner.is_active
    assert verify_password(PASSWORD, owner.password_hash)
    vendor = (await db.execute(select(EventVendor).where(EventVendor.business_id == biz.id))).scalar_one()
    assert (vendor.event_id, vendor.stall_code, vendor.category) == (ev["id"], None, "CLOTHING")
    # Created PENDING; the access reconcile then marks it UNPAID because today is an unpaid billable day.
    assert vendor.status == "UNPAID"
    # Today is inside the event and unpaid, so the business can log in (CONFIGURE) but not trade.
    assert v["access_level"] == "CONFIGURE" and biz.is_active is True
    user, _, access, refresh = await AuthService(db).login_email(v["email"], PASSWORD)
    assert access and refresh and user.business_id == biz.id
    with pytest.raises(AppError) as exc:
        await assert_trading_allowed(db, biz.id)
    assert exc.value.code == "EVENT_TRADING_NOT_ALLOWED"


async def test_registration_audit_row_holds_no_pii(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev, verify=False)
    rows = (await db.execute(select(AuditEvent).where(AuditEvent.business_id == v["business_id"]))).scalars().all()
    # Email verification added a second row (the verification email was sent).
    assert sorted(r.action for r in rows) == ["auth.email_verification_sent", "event.vendor_self_registered"]
    blob = "".join(repr(r.diff_json) + repr(r.target_type) for r in rows)
    assert v["email"] not in blob and PASSWORD not in blob and ev["code"] not in blob
    assert {r.actor_user_id for r in rows} == {v["owner_id"]}


async def test_duplicate_email_gets_the_same_answer_as_a_new_registration_and_creates_nothing(db, bag):
    # Changed with email verification (owner request: no email enumeration).
    # Was: 409 REGISTRATION_FAILED. Now the duplicate gets the exact body a
    # new registration gets, and still creates nothing.
    ev = await _open_event(db, bag)
    first = await _register(db, bag, ev)
    body = _body(bag, ev["code"], owner_email=first["email"].upper())
    bag["emails"].append(first["email"])
    before = (await db.execute(select(func.count()).select_from(Business))).scalar()
    res = await _REGISTER(ev["slug"], body, None, db)
    assert res == first["response"]
    assert (await db.execute(select(func.count()).select_from(Business))).scalar() == before


async def test_registration_input_policy(db, bag):
    ev = await _open_event(db, bag)
    for bad in (dict(password="short"), dict(password="x" * 129), dict(password="é" * 40),  # 80 bytes > bcrypt's 72
                dict(accept_terms=False), dict(category="PLUMBING"), dict(owner_email="not-an-email"),
                dict(business_name=" ")):
        with pytest.raises(ValidationError):
            _body(bag, ev["code"], **bad)


async def test_far_future_registration_is_suspended_until_the_prep_window(db, bag):
    far = [TODAY + timedelta(days=60), TODAY + timedelta(days=61)]
    ev = await _open_event(db, bag, days=far)
    v = await _register(db, bag, ev)
    assert v["access_level"] == "NONE" and v["login_opens_on"] == far[0] - timedelta(days=14)
    biz = (await db.execute(select(Business).where(Business.id == v["business_id"]))).scalar_one()
    assert biz.is_active is False
    vendor = (await db.execute(select(EventVendor).where(EventVendor.business_id == biz.id))).scalar_one()
    assert vendor.status == "PENDING" and vendor.stall_code is None


# ── Vendor self-service subscriptions ─────────────────────────────────────────

async def test_vendor_quotes_prices_from_rules_only_and_lists_options(db, bag):
    ev = await _open_event(db, bag)
    f = await _register(db, bag, ev, "FOOD")
    c = await _register(db, bag, ev, "CLOTHING")
    sub_f = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[2]), f["user"], db)
    sub_c = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[2]), c["user"], db)
    assert (sub_f["status"], sub_f["billable_days"], sub_f["amount_cents"]) == ("UNPAID", 3, 30_000)
    assert (sub_c["amount_cents"], sub_c["day_rate_cents"]) == (15_000, 5_000)
    listing = await ven.list_event_subscriptions(f["user"], db)
    [row] = listing["events"]
    assert row["event_id"] == ev["id"] and row["category"] == "FOOD" and row["can_subscribe"] is True
    assert row["quote_options"] == {"day_rate_cents": 10_000, "billable_days": DAYS, "billable_day_count": 6}
    assert [s["id"] for s in row["subscriptions"]] == [sub_f["id"]]
    assert "amount_cents" not in ven.SubscriptionRequest.model_fields  # the client never sends a price


async def test_a_vendor_cannot_quote_for_an_event_it_is_not_registered_at(db, bag):
    ev1, ev2 = await _open_event(db, bag), await _open_event(db, bag)
    v = await _register(db, bag, ev1)
    err = await _refused(ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev2["id"], starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db))
    ghost = await _refused(ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=uuid.uuid4(), starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db))
    assert err.status_code == ghost.status_code == 404
    assert (await db.execute(select(func.count()).select_from(EventVendorSubscription).where(
        EventVendorSubscription.business_id == v["business_id"]))).scalar() == 0


async def test_range_without_billable_days_and_cancelled_events_are_refused(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    far = DAYS[-1] + timedelta(days=30)
    assert (await _refused(ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=far, ends_on=far), v["user"], db))).code == "INVALID_SUBSCRIPTION_RANGE"
    assert (await _refused(ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[1], ends_on=DAYS[0]), v["user"], db))).status_code == 422
    await set_event_status(ev["id"], EventStatusUpdate(status="CANCELLED"), ADMIN, db)
    # A cancelled event cancels its vendors (reconcile), so the vendor is no longer a live vendor there.
    assert (await _refused(ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db))).status_code in (404, 409)


async def test_overlapping_subscriptions_are_refused_but_void_and_adjacent_are_fine(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    mk = lambda a, b: ven.create_event_subscription(  # noqa: E731
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[a], ends_on=DAYS[b]), v["user"], db)
    first = await mk(1, 2)
    for a, b in ((1, 2), (0, 1), (2, 3), (0, 5), (2, 2)):
        assert (await _refused(mk(a, b))).code == "OVERLAPS_EXISTING"
    await mk(3, 4)  # adjacent, not overlapping
    await mk(0, 0)
    await ven.cancel_subscription(first["id"], v["user"], db)
    await mk(1, 2)  # the void one no longer blocks


async def test_admin_create_subscription_applies_the_same_overlap_rule(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    vendor_id = (await db.execute(select(EventVendor.id).where(EventVendor.business_id == v["business_id"]))).scalar_one()
    await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[1]), v["user"], db)
    err = await _refused(create_subscription(ev["id"], vendor_id, SubscriptionCreate(starts_on=DAYS[1], ends_on=DAYS[2]), ADMIN, db))
    assert err.code == "OVERLAPS_EXISTING" and err.status_code == 409
    ok = await create_subscription(ev["id"], vendor_id, SubscriptionCreate(starts_on=DAYS[2], ends_on=DAYS[3]), ADMIN, db)
    assert ok["status"] == "UNPAID"


# ── Tenant isolation ──────────────────────────────────────────────────────────

async def test_vendor_a_cannot_see_claim_or_cancel_vendor_bs_subscription(db, bag):
    ev = await _open_event(db, bag)
    a = await _register(db, bag, ev)
    b = await _register(db, bag, ev)
    sub_b = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[1]), b["user"], db)
    # Listing
    listing_a = await ven.list_event_subscriptions(a["user"], db)
    assert [s for e in listing_a["events"] for s in e["subscriptions"]] == []
    assert str(sub_b["id"]) not in repr(listing_a)
    # Claim / cancel B's id as A: the same 404 an unknown id gives.
    unknown = await _refused(ven.claim_payment(uuid.uuid4(), ven.PaymentClaim(reference="X-1"), a["user"], db))
    for call in (ven.claim_payment(sub_b["id"], ven.PaymentClaim(reference="X-1"), a["user"], db),
                 ven.cancel_subscription(sub_b["id"], a["user"], db)):
        err = await _refused(call)
        assert err.status_code == 404
        assert (err.status_code, err.code) == (unknown.status_code, unknown.code)
    await db.rollback()
    row = (await db.execute(select(EventVendorSubscription).where(EventVendorSubscription.id == sub_b["id"]))).scalar_one()
    assert row.status == "UNPAID" and row.payment_claim_reference is None
    # A's own listing still works and B's is untouched in B's view.
    assert [s["id"] for e in (await ven.list_event_subscriptions(b["user"], db))["events"] for s in e["subscriptions"]] == [sub_b["id"]]


async def test_a_business_with_no_event_sees_nothing(db, bag, test_business):
    assert (await ven.list_event_subscriptions(_owner(test_business.id), db)) == {"events": []}


async def test_self_service_routes_require_owner_or_manager_and_are_mounted():
    from backend.app.main import app
    for route in ven.router.routes:
        calls = [d.call for d in route.dependant.dependencies]
        assert any(c.__qualname__ == require_owner_or_manager.__qualname__ for c in calls), route.path
    paths = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
    assert ("/v1/business/event-subscriptions", ("GET",)) in paths
    assert ("/v1/business/event-subscriptions", ("POST",)) in paths
    assert ("/v1/business/event-subscriptions/{subscription_id}/payment-claim", ("POST",)) in paths
    assert ("/v1/business/event-subscriptions/{subscription_id}/cancel", ("POST",)) in paths
    assert ("/v1/public/events/{slug}", ("GET",)) in paths
    assert ("/v1/public/events/{slug}/register", ("POST",)) in paths
    assert ("/admin/events/{event_id}/registration", ("POST",)) not in paths  # admin plane is a separate app


async def test_public_routes_are_rate_limited_per_ip_with_tight_limits():
    for fn, expected in ((_INFO, "30"), (_REGISTER, "5")):
        name = f"{fn.__module__}.{fn.__name__}"
        configured = limiter._route_limits.get(name, [])
        assert configured, f"no rate limit configured for {name}"
        assert any(expected in str(item.limit) for item in configured)
    reg_limits = [str(i.limit) for i in limiter._route_limits[f"{_REGISTER.__module__}.{_REGISTER.__name__}"]]
    assert any("hour" in s for s in reg_limits)  # a daily-ish ceiling as well as the per-minute one


# ── Payment claim and admin confirmation ──────────────────────────────────────

async def test_claim_is_recorded_not_paid_idempotent_and_correctable(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[1]), v["user"], db)
    first = await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="  EFT-777  "), v["user"], db)
    assert first["payment_claim_reference"] == "EFT-777" and first["payment_claimed_at"] and first["already_claimed"] is False
    assert first["status"] == "UNPAID" and first["paid_at"] is None
    again = await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-777"), v["user"], db)
    assert again["already_claimed"] is True and again["payment_claimed_at"] == first["payment_claimed_at"]
    fixed = await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-778"), v["user"], db)
    assert fixed["payment_claim_reference"] == "EFT-778"
    access = await get_event_access(v["user"], db)
    assert access["access_level"] == "CONFIGURE"  # a claim grants nothing
    with pytest.raises(ValidationError):
        ven.PaymentClaim(reference="   ")


async def test_admin_sees_the_claim_and_mark_paid_flips_access_to_trade(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[1]), v["user"], db)
    await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-900"), v["user"], db)
    [item] = (await list_vendors(ev["id"], ADMIN, db))["items"]
    [s] = item["subscriptions"]
    assert (s["payment_claim_reference"], s["status"]) == ("EFT-900", "UNPAID") and s["payment_claimed_at"]
    assert (await get_event_access(v["user"], db))["access_level"] == "CONFIGURE"
    await mark_subscription_paid(ev["id"], sub["id"], MarkPaid(payment_reference="EFT-900"), ADMIN, db)
    access = await get_event_access(v["user"], db)
    assert access["access_level"] == "TRADE" and access["reason"] == "PAID_DAY"
    await assert_trading_allowed(db, v["business_id"])  # today is a paid billable day
    # Once paid, the vendor can no longer claim or cancel it.
    for call in (ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-901"), v["user"], db),
                 ven.cancel_subscription(sub["id"], v["user"], db)):
        assert (await _refused(call)).code == "SUBSCRIPTION_NOT_UNPAID"


async def test_a_reference_already_used_on_a_paid_subscription_cannot_be_claimed(db, bag):
    ev = await _open_event(db, bag)
    a, b = await _register(db, bag, ev), await _register(db, bag, ev)
    sub_a = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[0]), a["user"], db)
    sub_b = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[1], ends_on=DAYS[1]), b["user"], db)
    await mark_subscription_paid(ev["id"], sub_a["id"], MarkPaid(payment_reference="EFT-USED"), ADMIN, db)
    err = await _refused(ven.claim_payment(sub_b["id"], ven.PaymentClaim(reference="EFT-USED"), b["user"], db))
    assert (err.code, err.status_code) == ("PAYMENT_REFERENCE_USED", 409)
    await db.rollback()
    row = (await db.execute(select(EventVendorSubscription).where(EventVendorSubscription.id == sub_b["id"]))).scalar_one()
    assert row.payment_claim_reference is None


async def test_vendor_cancels_own_unpaid_subscription_idempotently(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db)
    assert (await ven.cancel_subscription(sub["id"], v["user"], db))["status"] == "VOID"
    assert (await ven.cancel_subscription(sub["id"], v["user"], db))["status"] == "VOID"
    assert (await _refused(ven.claim_payment(sub["id"], ven.PaymentClaim(reference="Z"), v["user"], db))).code == "SUBSCRIPTION_NOT_UNPAID"
    assert (await _refused(mark_subscription_paid(ev["id"], sub["id"], MarkPaid(payment_reference="Z"), ADMIN, db))).code == "SUBSCRIPTION_VOID"


async def test_self_service_works_while_access_is_configure_and_not_through_the_trading_gate(db, bag):
    ev = await _open_event(db, bag)
    v = await _register(db, bag, ev)
    assert (await get_event_access(v["user"], db))["access_level"] == "CONFIGURE"
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev["id"], starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db)
    await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="CFG-1"), v["user"], db)
    await ven.cancel_subscription(sub["id"], v["user"], db)
    import inspect
    assert "assert_trading_allowed" not in inspect.getsource(ven)


# ── Migration/model parity ────────────────────────────────────────────────────

async def test_0064_columns_exist_with_the_expected_defaults(db, bag):
    ev = await _open_event(db, bag, registration=False)
    row = (await db.execute(select(Event).where(Event.id == ev["id"]))).scalar_one()
    assert row.registration_open is False and row.registration_code_hash is None
