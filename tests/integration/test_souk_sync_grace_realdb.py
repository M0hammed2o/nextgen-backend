"""
Real-Postgres tests for BUG-0208: the post-event sync grace, the sync-only
allowlist (through the real FastAPI app and real JWTs), and the late-upload
review queue (migration 0069), including concurrent duplicates.

Route functions are called directly with real sessions where only the route
logic matters; the allowlist is exercised over HTTP (httpx ASGITransport,
get_db overridden to a real session) because it depends on the matched route.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update
from starlette.responses import JSONResponse

from admin_api.app.api.v1.routes_events import (
    EventStatusUpdate,
    VendorSuspend,
    cancel_vendor,
    set_event_status,
    suspend_vendor,
)
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    OfflineReviewDecisionRequest,
    POSOrderCreateRequest,
    POSOrderItemRequest,
    TillOpenRequest,
    approve_offline_review,
    create_pos_order,
    list_flagged_offline_orders,
    list_offline_reviews,
    open_till,
    reject_offline_review,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import create_access_token, hash_pin
from backend.app.events.service import EVENT_SUSPENSION_REASON, reconcile_single_business_access
from backend.app.events.trading import EVENT_SYNC_GRACE, event_sync_grace_until
from backend.app.services.auth_service import AuthService
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import EventVendorSubscription
from shared.models.offline_review import OfflineCaptureReview
from shared.models.order import Order
from shared.models.pos_payment import POSPaymentAllocation
from shared.models.user import BusinessUser
from tests.integration._db import get_session_factory
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    ADMIN,
    SAST,
    TODAY,
    _biz,
    _event,
    _pay,
    _plain_business,
    _row,
    _vendors,
    bag,
)

pytestmark = pytest.mark.asyncio

PRICE = 5000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sast(day, hour=12, minute=0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SAST).astimezone(timezone.utc)


def _day(n: int):
    return TODAY + timedelta(days=n)


def _req(item_id, captured, key=None, **kw) -> POSOrderCreateRequest:
    kw.setdefault("payment_method", "CASH")
    if kw["payment_method"] == "CASH":
        kw.setdefault("cash_tendered_cents", 10_000_000)
    return POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=item_id, quantity=1)],
        offline_captured_at=captured, idempotency_key=key or f"grace-{uuid.uuid4().hex}", **kw,
    )


async def _err(coro) -> tuple[int, str]:
    with pytest.raises((AppError, NotFoundError)) as exc:
        await coro
    return exc.value.status_code, exc.value.code


def _json(resp: JSONResponse) -> dict:
    return json.loads(resp.body)


async def _owner_row(db, business_id) -> BusinessUser:
    return (await db.execute(select(BusinessUser).where(
        BusinessUser.business_id == business_id, BusinessUser.role == "OWNER"))).scalars().first()


async def _staff_row(db, business_id, role="STAFF", pin="4321") -> BusinessUser:
    u = BusinessUser(business_id=business_id, role=role, staff_name=f"{role.title()} {uuid.uuid4().hex[:4]}",
                     pin_hash=hash_pin(pin), is_active=True)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _auth(u: BusinessUser, sync_only_until=None) -> AuthUser:
    return AuthUser(user_id=u.id, business_id=u.business_id, role=u.role, token_type="access",
                    sync_only_until=sync_only_until)


async def _item(db, owner: AuthUser, price=PRICE):
    cat = await create_category(body=CategoryCreate(name="Cat"), user=owner, db=db)
    return await create_item(body=ItemCreate(category_id=cat.id, name="Lamb Wrap", price_cents=price),
                             user=owner, db=db)


async def _ended_vendor(db, bag, days, paid=None):
    """An event vendor whose (paid) event days are over, suspended by the real reconcile."""
    ev = await _event(db, bag, days=days)
    paid = paid or days
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=paid[0], ends=paid[-1])])
    await _pay(db, ev, v)
    bid = v["business_id"]
    await db.execute(update(Business).where(Business.id == bid).values(pos_enabled=True))
    await db.commit()
    await reconcile_single_business_access(db, bid)
    await db.commit()
    biz = await _biz(db, bid)
    assert biz.plan == EVENT_PLAN
    assert not biz.is_active and biz.suspended_reason == EVENT_SUSPENSION_REASON
    owner_row = await _owner_row(db, bid)
    grace = await event_sync_grace_until(db, bid)
    owner = _auth(owner_row, grace)
    item = await _item(db, owner)
    return ev, v, owner_row, owner, item, grace


async def _reviews(db, business_id) -> list[OfflineCaptureReview]:
    return list((await db.execute(select(OfflineCaptureReview).where(
        OfflineCaptureReview.business_id == business_id).execution_options(populate_existing=True))).scalars())


async def _orders(db, business_id, key=None) -> int:
    q = select(func.count(Order.id)).where(Order.business_id == business_id)
    if key:
        q = q.where(Order.idempotency_key == key)
    return (await db.execute(q)).scalar_one()


def _note(text="checked against the paper tally") -> OfflineReviewDecisionRequest:
    return OfflineReviewDecisionRequest(note=text)


# -- HTTP harness --------------------------------------------------------------

@contextlib.asynccontextmanager
async def _client():
    from backend.app.db.session import get_db
    from backend.app.main import app

    async def _override():
        async with get_session_factory()() as s:
            yield s

    app.dependency_overrides[get_db] = _override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


def _bearer(u: BusinessUser) -> dict:
    token = create_access_token(u.id, u.business_id, u.role, token_version=u.token_version or 0)
    return {"Authorization": f"Bearer {token}"}


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code") or (body.get("detail") or {}).get("code") if isinstance(body, dict) \
        else None


# == grace window ==============================================================

async def test_grace_runs_72h_from_the_end_of_the_last_paid_local_day(db, bag):
    _, v, _, _, _, grace = await _ended_vendor(db, bag, [_day(-1)])
    end_of_last_day = datetime.combine(TODAY, time(0), tzinfo=SAST).astimezone(timezone.utc)
    assert grace == end_of_last_day + EVENT_SYNC_GRACE
    bid = v["business_id"]
    assert await event_sync_grace_until(db, bid, grace - timedelta(seconds=1)) == grace
    assert await event_sync_grace_until(db, bid, grace) is None                      # expired exactly at the end
    assert await event_sync_grace_until(db, bid, end_of_last_day - timedelta(seconds=1)) is None  # not before


async def test_grace_is_anchored_to_the_end_of_the_event_when_only_early_days_were_paid(db, bag):
    # Review L-3: three-day event, vendor paid only the first day. It is suspended only after the
    # event's last day, so the grace runs from the end of that day, not from the end of the paid one.
    _, v, _, _, _, grace = await _ended_vendor(db, bag, [_day(-3), _day(-2), _day(-1)], paid=[_day(-3)])
    assert grace == datetime.combine(TODAY, time(0), tzinfo=SAST).astimezone(timezone.utc) + EVENT_SYNC_GRACE


async def test_a_vendor_that_never_paid_a_trading_day_gets_no_grace(db, bag):
    ev = await _event(db, bag, days=[_day(-1)])
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=_day(-1), ends=_day(-1))])  # never marked paid
    await reconcile_single_business_access(db, v["business_id"])
    await db.commit()
    biz = await _biz(db, v["business_id"])
    assert not biz.is_active and biz.suspended_reason == EVENT_SUSPENSION_REASON
    assert await event_sync_grace_until(db, v["business_id"]) is None


async def test_expired_grace_leaves_the_vendor_suspended(db, bag):
    _, v, owner_row, _, _, grace = await _ended_vendor(db, bag, [_day(-5)])
    assert grace is None
    async with _client() as c:
        r = await c.get("/v1/business/event-access", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


async def test_admin_suspended_vendor_gets_no_grace(db, bag):
    ev, v, owner_row, _, _, grace = await _ended_vendor(db, bag, [_day(-1)])
    assert grace is not None
    await suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="fraud check"), ADMIN, db)
    assert await event_sync_grace_until(db, v["business_id"]) is None
    async with _client() as c:
        r = await c.get("/v1/business/event-access", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


async def test_cancelled_vendor_gets_no_grace(db, bag):
    ev, v, owner_row, _, _, grace = await _ended_vendor(db, bag, [_day(-1)])
    assert grace is not None
    await cancel_vendor(ev["id"], v["vendor_id"], ADMIN, db)
    biz = await _biz(db, v["business_id"])
    assert not biz.is_active and biz.suspended_reason == EVENT_SUSPENSION_REASON  # same reason, still no grace
    assert await event_sync_grace_until(db, v["business_id"]) is None
    async with _client() as c:
        r = await c.get("/v1/business/event-access", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


async def test_cancelled_event_gives_its_vendors_no_grace(db, bag):
    ev, v, _, _, _, grace = await _ended_vendor(db, bag, [_day(-1)])
    assert grace is not None
    await set_event_status(ev["id"], EventStatusUpdate(status="CANCELLED"), ADMIN, db)
    assert await event_sync_grace_until(db, v["business_id"]) is None


async def test_non_event_business_never_gets_grace(db, bag):
    biz = await _plain_business(db, bag, pos_enabled=True)
    owner_row = await _staff_row(db, biz.id, role="OWNER")
    # even with the exact end-of-event reason (set by hand; the reconcile never does this to a restaurant)
    await db.execute(update(Business).where(Business.id == biz.id).values(
        is_active=False, suspended_reason=EVENT_SUSPENSION_REASON))
    await db.commit()
    assert await event_sync_grace_until(db, biz.id) is None
    async with _client() as c:
        r = await c.get("/v1/business/orders", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


async def test_restaurant_that_once_traded_at_a_souk_gets_no_grace(db, bag):
    # A business with real paid vendor rows at an ended event, but no longer on the EVENT plan,
    # carrying the exact end-of-event reason: the plan check alone must refuse the grace.
    _, v, owner_row, _, _, grace = await _ended_vendor(db, bag, [_day(-1)])
    assert grace is not None
    await db.execute(update(Business).where(Business.id == v["business_id"]).values(plan="STARTER"))
    await db.commit()
    assert await event_sync_grace_until(db, v["business_id"]) is None
    async with _client() as c:
        r = await c.get("/v1/business/orders", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


# == allowlist through the real app ============================================

async def test_allowlist_over_http_during_grace(db, bag):
    ev, v, owner_row, owner, item, grace = await _ended_vendor(db, bag, [_day(-1)])
    oid = uuid.uuid4()
    online = {"items": [{"menu_item_id": str(item.id), "quantity": 1}], "payment_method": "CASH",
              "cash_tendered_cents": 100_000}
    async with _client() as c:
        h = _bearer(owner_row)
        r = await c.get("/v1/business/event-access", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sync_only"] is True and body["access_level"] == "NONE"
        assert datetime.fromisoformat(body["sync_only_until"]) == grace
        assert (await c.get("/v1/auth/me", headers=h)).status_code == 200
        assert (await c.get("/v1/business/orders", headers=h)).status_code == 200
        assert (await c.get("/v1/business/pos/till/current", headers=h)).status_code == 200
        assert (await c.get("/v1/business/pos/offline-orders/flagged", headers=h)).status_code == 200
        assert (await c.get("/v1/business/pos/offline-reviews", headers=h)).status_code == 200
        refused = [
            await c.post("/v1/business/pos/orders", json=online, headers=h),                     # online sale
            await c.post("/v1/business/menu/categories", json={"name": "New"}, headers=h),      # menu edit
            await c.put(f"/v1/business/menu/items/{item.id}", json={"name": "x"}, headers=h),
            await c.post("/v1/business/staff", json={"staff_name": "X", "role": "STAFF"}, headers=h),  # staff
            await c.post(f"/v1/business/pos/orders/{oid}/refund", json={}, headers=h),          # refund
            await c.post(f"/v1/business/pos/orders/{oid}/void", json={}, headers=h),
            await c.patch(f"/v1/business/orders/{oid}/payment", json={}, headers=h),            # payment
            await c.post("/v1/business/pos/payment-intents", json={}, headers=h),
            await c.post("/v1/business/pos/till/open", json={"opening_float_cents": 0}, headers=h),
            await c.post("/v1/business/event-subscriptions", json={}, headers=h),
            await c.post("/v1/business/orders", json={}, headers=h),
        ]
        for r in refused:
            assert r.status_code == 403 and _code(r) == "EVENT_SYNC_ONLY", (r.request.url, r.text)
        # an offline upload of a final-day sale goes through the real auth path
        offline = {**online, "offline_captured_at": _sast(_day(-1), 21).isoformat(),
                   "idempotency_key": f"http-{uuid.uuid4().hex}"}
        r = await c.post("/v1/business/pos/orders", json=offline, headers=h)
        # Review M-1: in grace it is held for review even without the opt-in flag
        assert r.status_code == 202, r.text
        assert r.json()["code"] == "OFFLINE_CAPTURE_PENDING_REVIEW"


async def test_staff_pin_login_works_in_grace_but_not_after_admin_suspension(db, bag):
    ev, v, _, _, _, _ = await _ended_vendor(db, bag, [_day(-1)])
    staff = await _staff_row(db, v["business_id"], pin="2468")
    biz = await _biz(db, v["business_id"])
    _, _, access, _ = await AuthService(db).login_pin(biz.business_code, staff.id, "2468")
    assert access
    await suspend_vendor(ev["id"], v["vendor_id"], VendorSuspend(reason="hold"), ADMIN, db)
    assert await _err(AuthService(db).login_pin(biz.business_code, staff.id, "2468")) == (403, "BUSINESS_SUSPENDED")


# == offline sync during grace ================================================

async def test_final_day_sale_uploaded_in_grace_is_held_for_review_then_approved(db, bag):
    # Review M-1: a last-paid-day capture within 36h, uploaded in grace, posts NOTHING until approved.
    _, v, _, owner, item, grace = await _ended_vendor(db, bag, [_day(-1)])
    key = f"final-{uuid.uuid4().hex}"
    captured = _sast(_day(-1), 22)
    first = await create_pos_order(_req(item.id, captured, key), owner, db)  # no opt-in flag at all
    again = await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True), owner, db)
    assert first.status_code == again.status_code == 202
    assert _json(first)["review_id"] == _json(again)["review_id"]
    assert await _orders(db, v["business_id"]) == 0
    allocs = (await db.execute(select(func.count(POSPaymentAllocation.id)).where(
        POSPaymentAllocation.business_id == v["business_id"]))).scalar_one()
    assert allocs == 0
    [review] = await _reviews(db, v["business_id"])
    assert {"post_event_grace_upload", "post_event_grace_sync"} <= set(review.upload_review_reasons)
    assert "capture_day_unpaid_at_sync" not in review.upload_review_reasons

    out = await approve_offline_review(review.id, _note(), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.order.id))).scalar_one()
    assert {"post_event_grace_upload", "post_event_grace_sync", "late_capture_approved"} <= set(
        row.offline_review_reasons)
    assert row.idempotency_key == key and row.offline_captured_at == captured
    replay = await create_pos_order(_req(item.id, captured, key), owner, db)
    assert replay.id == out.order.id and await _orders(db, v["business_id"], key) == 1
    flagged = await list_flagged_offline_orders(reason="post_event_grace_sync", since=None,
                                                include_acknowledged=False, limit=50, user=owner, db=db)
    assert [f.id for f in flagged] == [out.order.id]


async def test_concurrent_duplicate_uploads_in_grace_make_one_review_and_no_order(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-1)])
    key = f"cc-{uuid.uuid4().hex}"
    captured = _sast(_day(-1), 20)

    async def sync():
        async with get_session_factory()() as s:
            return await create_pos_order(_req(item.id, captured, key), owner, s)

    results = await asyncio.gather(*(sync() for _ in range(3)))
    assert {r.status_code for r in results} == {202}
    assert len({_json(r)["review_id"] for r in results}) == 1
    assert len(await _reviews(db, v["business_id"])) == 1 and await _orders(db, v["business_id"]) == 0


async def test_outside_grace_a_paid_day_offline_sync_still_posts_directly(db, bag):
    # Normal trading (business active, today is a paid day): unchanged, an order at once, no review.
    ev = await _event(db, bag, days=[TODAY])
    [v] = await _vendors(db, ev, bag, [_row(bag, starts=TODAY, ends=TODAY)])
    await _pay(db, ev, v)
    await db.execute(update(Business).where(Business.id == v["business_id"]).values(pos_enabled=True))
    await db.commit()
    assert (await _biz(db, v["business_id"])).is_active
    owner = _auth(await _owner_row(db, v["business_id"]))
    assert owner.sync_only_until is None and await event_sync_grace_until(db, v["business_id"]) is None
    item = await _item(db, owner)
    captured = max(_sast(TODAY, 0, 1), _now() - timedelta(minutes=30))
    out = await create_pos_order(_req(item.id, captured, offline_review_if_too_old=True), owner, db)
    assert out.is_offline_capture and out.payment_status == "PAID"
    assert await _reviews(db, v["business_id"]) == []


async def test_unpaid_day_capture_is_still_refused_in_grace_even_with_review_opt_in(db, bag):
    _, v, _, owner, item, grace = await _ended_vendor(db, bag, [_day(-2), _day(-1)], paid=[_day(-1)])
    assert grace is not None
    code = await _err(create_pos_order(
        _req(item.id, _sast(_day(-2), 14), offline_review_if_too_old=True), owner, db))
    assert code == (403, "EVENT_TRADING_NOT_ALLOWED")
    assert await _reviews(db, v["business_id"]) == [] and await _orders(db, v["business_id"]) == 0


async def test_future_dated_capture_is_refused_and_never_queued(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-1)])
    code = await _err(create_pos_order(
        _req(item.id, _now() + timedelta(hours=2), offline_review_if_too_old=True), owner, db))
    assert code == (422, "OFFLINE_CAPTURE_IN_FUTURE")
    assert await _reviews(db, v["business_id"]) == []


# == late uploads -> review queue ==============================================

async def test_late_paid_day_capture_in_grace_goes_to_review_not_auto_accept(db, bag):
    # paid day two days ago, synced in grace but more than 36h after capture
    _, v, _, owner, item, grace = await _ended_vendor(db, bag, [_day(-2)])
    assert grace is not None
    captured = _sast(_day(-2), 8)
    assert _now() - captured > timedelta(hours=36)
    # without the opt-in: exactly the old 422
    assert await _err(create_pos_order(_req(item.id, captured), owner, db)) == (422, "OFFLINE_CAPTURE_TOO_OLD")
    assert await _reviews(db, v["business_id"]) == []
    # with the opt-in: 202, a pending review, nothing financial
    key = f"late-{uuid.uuid4().hex}"
    resp = await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True,
                                       offline_client_total_cents=PRICE), owner, db)
    assert isinstance(resp, JSONResponse) and resp.status_code == 202
    body = _json(resp)
    assert body["code"] == "OFFLINE_CAPTURE_PENDING_REVIEW" and body["review_status"] == "PENDING"
    [review] = await _reviews(db, v["business_id"])
    assert str(review.id) == body["review_id"] and review.idempotency_key == key
    assert review.captured_at == captured and review.status == "PENDING"
    assert review.synced_by_user_id == owner.user_id and review.synced_by_role == "OWNER"
    assert "post_event_grace_sync" in review.upload_review_reasons
    assert review.payload["offline_client_total_cents"] == PRICE
    assert await _orders(db, v["business_id"]) == 0
    allocs = (await db.execute(select(func.count(POSPaymentAllocation.id)).where(
        POSPaymentAllocation.business_id == v["business_id"]))).scalar_one()
    assert allocs == 0
    # re-upload (with or without the flag): same review, still nothing financial
    again = await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True), owner, db)
    old_client = await create_pos_order(_req(item.id, captured, key), owner, db)
    assert _json(again)["review_id"] == _json(old_client)["review_id"] == body["review_id"]
    assert len(await _reviews(db, v["business_id"])) == 1 and await _orders(db, v["business_id"]) == 0


async def test_concurrent_duplicate_late_uploads_make_one_review(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    key = f"ccq-{uuid.uuid4().hex}"
    captured = _sast(_day(-2), 9)

    async def upload():
        async with get_session_factory()() as s:
            return await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True), owner, s)

    results = await asyncio.gather(*(upload() for _ in range(4)))
    assert {r.status_code for r in results} == {202}
    assert len({_json(r)["review_id"] for r in results}) == 1
    assert len(await _reviews(db, v["business_id"])) == 1 and await _orders(db, v["business_id"]) == 0


async def _queued(db, owner, item, captured, **kw) -> tuple[str, uuid.UUID]:
    key = f"q-{uuid.uuid4().hex}"
    resp = await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True, **kw), owner, db)
    assert resp.status_code == 202, resp
    return key, uuid.UUID(_json(resp)["review_id"])


async def test_approve_creates_one_flagged_order_through_the_normal_path(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    captured = _sast(_day(-2), 10)
    key, rid = await _queued(db, owner, item, captured)
    pending = await list_offline_reviews(status="PENDING", limit=50, user=owner, db=db)
    assert pending.pending_count == 1 and pending.items[0].id == rid
    assert pending.items[0].lines[0].name == "Lamb Wrap"

    out = await approve_offline_review(rid, _note(), owner, db)
    assert out.review.status == "APPROVED" and out.review.decision_note == "checked against the paper tally"
    assert out.review.decided_by_user_id == owner.user_id
    order = out.order
    assert order is not None and order.total_cents == PRICE and order.payment_status == "PAID"
    row = (await db.execute(select(Order).where(Order.id == order.id))).scalar_one()
    assert row.idempotency_key == key and row.offline_captured_at == captured
    assert {"late_capture_approved", "post_event_grace_sync", "late_sync"} <= set(row.offline_review_reasons)
    assert row.offline_unattributed is True and row.till_session_id is None  # no till covered the capture
    assert row.offline_synced_by_user_id == owner.user_id
    flagged = await list_flagged_offline_orders(reason="late_capture_approved", since=None,
                                                include_acknowledged=False, limit=50, user=owner, db=db)
    assert [f.id for f in flagged] == [order.id]
    # idempotent: approve again -> same order; a re-upload of the key replays the order
    again = await approve_offline_review(rid, _note("again"), owner, db)
    assert again.order.id == order.id and again.review.decision_note == "checked against the paper tally"
    replay = await create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True), owner, db)
    assert replay.id == order.id
    assert await _orders(db, v["business_id"], key) == 1
    assert await _err(reject_offline_review(rid, _note(), owner, db)) == (409, "OFFLINE_REVIEW_ALREADY_DECIDED")
    assert (await list_offline_reviews(status="PENDING", limit=50, user=owner, db=db)).pending_count == 0


async def test_reject_has_no_financial_effect_and_is_final(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    captured = _sast(_day(-2), 11)
    key, rid = await _queued(db, owner, item, captured)
    out = await reject_offline_review(rid, _note("duplicate of a manual entry"), owner, db)
    assert out.review.status == "REJECTED" and out.order is None
    assert await _orders(db, v["business_id"]) == 0
    again = await reject_offline_review(rid, _note("second"), owner, db)
    assert again.review.decision_note == "duplicate of a manual entry"
    assert await _err(approve_offline_review(rid, _note(), owner, db)) == (409, "OFFLINE_REVIEW_ALREADY_DECIDED")
    code = await _err(create_pos_order(_req(item.id, captured, key, offline_review_if_too_old=True), owner, db))
    assert code == (409, "OFFLINE_CAPTURE_REVIEW_REJECTED")
    assert await _orders(db, v["business_id"]) == 0


async def test_two_managers_approving_at_once_make_exactly_one_order(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    manager = _auth(await _staff_row(db, v["business_id"], role="MANAGER"), owner.sync_only_until)
    key, rid = await _queued(db, owner, item, _sast(_day(-2), 12))

    async def approve(who):
        async with get_session_factory()() as s:
            return await approve_offline_review(rid, _note(f"by {who.role}"), who, s)

    a, b, c = await asyncio.gather(approve(owner), approve(manager), approve(owner))
    assert a.order.id == b.order.id == c.order.id
    assert await _orders(db, v["business_id"], key) == 1


async def test_concurrent_approve_and_reject_yield_one_decision(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    key, rid = await _queued(db, owner, item, _sast(_day(-2), 13))

    async def decide(fn):
        async with get_session_factory()() as s:
            try:
                return await fn(rid, _note(), owner, s)
            except AppError as exc:
                return exc.code

    results = await asyncio.gather(decide(approve_offline_review), decide(reject_offline_review))
    [review] = await _reviews(db, v["business_id"])
    assert "OFFLINE_REVIEW_ALREADY_DECIDED" in results
    assert await _orders(db, v["business_id"], key) == (1 if review.status == "APPROVED" else 0)


async def test_approval_revalidates_the_paid_day(db, bag):
    ev, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    key, rid = await _queued(db, owner, item, _sast(_day(-2), 14))
    # the payment is voided after the upload: the capture day is no longer a paid TRADE day
    await db.execute(update(EventVendorSubscription).where(
        EventVendorSubscription.business_id == v["business_id"]).values(status="VOID", paid_at=None))
    await db.commit()
    assert await _err(approve_offline_review(rid, _note(), owner, db)) == (403, "EVENT_TRADING_NOT_ALLOWED")
    await db.rollback()
    [review] = await _reviews(db, v["business_id"])
    assert review.status == "PENDING" and await _orders(db, v["business_id"]) == 0


async def test_cross_tenant_review_ids_are_404(db, bag):
    _, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    _, rid = await _queued(db, owner, item, _sast(_day(-2), 15))
    other = await _plain_business(db, bag, pos_enabled=True)
    stranger = _auth(await _staff_row(db, other.id, role="OWNER"))
    assert await _err(approve_offline_review(rid, _note(), stranger, db)) == (404, "OFFLINE_REVIEW_NOT_FOUND")
    assert await _err(reject_offline_review(rid, _note(), stranger, db)) == (404, "OFFLINE_REVIEW_NOT_FOUND")
    listed = await list_offline_reviews(status="ALL", limit=50, user=stranger, db=db)
    assert listed.items == [] and listed.pending_count == 0
    [review] = await _reviews(db, v["business_id"])
    assert review.status == "PENDING"


# == restaurants (non-event) ===================================================

async def _restaurant(db, bag, max_staff_discount_cents=0):
    biz = await _plain_business(db, bag, pos_enabled=True, max_staff_discount_cents=max_staff_discount_cents)
    owner_row = await _staff_row(db, biz.id, role="OWNER")
    owner = _auth(owner_row)
    return biz, owner_row, owner, await _item(db, owner)


async def test_restaurant_offline_sync_is_unchanged(db, bag):
    biz, _, owner, item = await _restaurant(db, bag)
    out = await create_pos_order(_req(item.id, _now() - timedelta(hours=2)), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.id))).scalar_one()
    assert row.offline_review_reasons == []
    assert await _err(create_pos_order(_req(item.id, _now() - timedelta(hours=80)), owner, db)) == (
        422, "OFFLINE_CAPTURE_TOO_OLD")
    assert await _reviews(db, biz.id) == []


async def test_restaurant_review_queue_and_till_attribution_on_approval(db, bag):
    biz, _, owner, item = await _restaurant(db, bag)
    till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    from shared.models.till import TillSession
    captured = _now() - timedelta(hours=80)
    await db.execute(update(TillSession).where(TillSession.id == till.id).values(
        opened_at=captured - timedelta(hours=1)))
    await db.commit()
    key, rid = await _queued(db, owner, item, captured)
    assert await _orders(db, biz.id) == 0
    out = await approve_offline_review(rid, _note(), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.order.id))).scalar_one()
    assert row.till_session_id == till.id and row.offline_unattributed is False
    assert "late_capture_approved" in row.offline_review_reasons
    assert "post_event_grace_sync" not in row.offline_review_reasons


async def test_suspended_restaurant_is_unaffected_by_grace(db, bag):
    biz, owner_row, _, _ = await _restaurant(db, bag)
    await db.execute(update(Business).where(Business.id == biz.id).values(
        is_active=False, suspended_reason="Payment overdue"))
    await db.commit()
    async with _client() as c:
        r = await c.get("/v1/business/pos/till/current", headers=_bearer(owner_row))
    assert r.status_code == 403 and _code(r) == "BUSINESS_SUSPENDED"


async def test_approval_keeps_the_h1_discount_rule(db, bag):
    biz, _, owner, item = await _restaurant(db, bag, max_staff_discount_cents=100)
    biz_id = biz.id  # the object expires on the rollbacks below
    staff = _auth(await _staff_row(db, biz_id, role="STAFF"))
    captured = _now() - timedelta(hours=80)
    # a STAFF upload carrying a discount above the staff limit queues (nothing is judged at upload) ...
    key, rid = await _queued(db, staff, item, captured, discount_cents=1000)
    # ... but an OWNER approving it does not lift the uploader's limit
    assert await _err(approve_offline_review(rid, _note(), owner, db)) == (403, "DISCOUNT_LIMIT_EXCEEDED")
    await db.rollback()
    # an OWNER upload naming a STAFF cashier is limited to the cashier's authority too
    key2, rid2 = await _queued(db, owner, item, captured, discount_cents=1000, captured_by_user_id=staff.user_id)
    assert await _err(approve_offline_review(rid2, _note(), owner, db)) == (403, "DISCOUNT_LIMIT_EXCEEDED")
    await db.rollback()
    statuses = {r.id: r.status for r in await _reviews(db, biz_id)}
    assert statuses == {rid: "PENDING", rid2: "PENDING"} and await _orders(db, biz_id) == 0
    # within the limit it approves, and the discount approver stays the uploader (never the approver)
    key3, rid3 = await _queued(db, staff, item, captured, discount_cents=50)
    out = await approve_offline_review(rid3, _note(), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.order.id))).scalar_one()
    assert row.discount_approved_by_user_id == staff.user_id and row.discount_cents == 50


async def test_unknown_cashier_is_refused_and_not_queued(db, bag):
    biz, _, owner, item = await _restaurant(db, bag)
    code = await _err(create_pos_order(_req(item.id, _now() - timedelta(hours=80), offline_review_if_too_old=True,
                                            captured_by_user_id=uuid.uuid4()), owner, db))
    assert code == (404, "USER_NOT_FOUND")
    assert await _reviews(db, biz.id) == []


def test_note_is_required():
    with pytest.raises(ValidationError):
        OfflineReviewDecisionRequest(note=" ")


# == reviewer follow-ups (L-1, L-2, L-3 admin count, info-1, info-2) ==========

async def test_inactive_uploader_keeps_only_staff_discount_authority(db, bag):
    # Review L-1: an OWNER who uploaded and has since been deactivated approves nothing above STAFF limits.
    biz, _, owner, item = await _restaurant(db, bag, max_staff_discount_cents=100)
    biz_id = biz.id
    second_owner_row = await _staff_row(db, biz_id, role="OWNER")
    key, rid = await _queued(db, _auth(second_owner_row), item, _now() - timedelta(hours=80), discount_cents=1000)
    await db.execute(update(BusinessUser).where(BusinessUser.id == second_owner_row.id).values(is_active=False))
    await db.commit()
    assert await _err(approve_offline_review(rid, _note(), owner, db)) == (403, "DISCOUNT_LIMIT_EXCEEDED")
    await db.rollback()
    assert {r.status for r in await _reviews(db, biz_id)} == {"PENDING"}


async def test_cashier_deactivated_after_upload_can_still_be_approved_at_staff_authority(db, bag):
    # Review L-2: validated at upload; deactivation afterwards (temp event staff) does not block approval.
    biz, _, owner, item = await _restaurant(db, bag, max_staff_discount_cents=100)
    biz_id = biz.id
    # an OWNER cashier: only OWNER is exempt from the staff discount limit, so the STAFF downgrade decides
    cashier = await _staff_row(db, biz_id, role="OWNER")
    captured = _now() - timedelta(hours=80)
    _, ok_rid = await _queued(db, owner, item, captured, discount_cents=50, captured_by_user_id=cashier.id)
    _, big_rid = await _queued(db, owner, item, captured, discount_cents=1000, captured_by_user_id=cashier.id)
    await db.execute(update(BusinessUser).where(BusinessUser.id == cashier.id).values(is_active=False))
    await db.commit()
    out = await approve_offline_review(ok_rid, _note(), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.order.id))).scalar_one()
    assert row.created_by_user_id == cashier.id
    assert "cashier_differs_from_syncer" in row.offline_review_reasons
    # an inactive OWNER cashier counts as STAFF: above the staff limit is refused
    assert await _err(approve_offline_review(big_rid, _note(), owner, db)) == (403, "DISCOUNT_LIMIT_EXCEEDED")


async def test_approval_prices_at_the_current_menu_price_and_flags_the_variance(db, bag):
    # Same as a normal offline sync: server price at the time it is recorded; the device total
    # is kept and the difference flagged.
    biz, _, owner, item = await _restaurant(db, bag)
    key, rid = await _queued(db, owner, item, _now() - timedelta(hours=80), offline_client_total_cents=PRICE)
    from shared.models.menu import MenuItem
    await db.execute(update(MenuItem).where(MenuItem.id == item.id).values(price_cents=PRICE + 700))
    await db.commit()
    out = await approve_offline_review(rid, _note(), owner, db)
    row = (await db.execute(select(Order).where(Order.id == out.order.id))).scalar_one()
    assert row.total_cents == PRICE + 700
    assert row.offline_price_variance_cents == -700 and row.offline_review_required


async def test_admin_vendor_list_shows_pending_late_uploads(db, bag):
    # Review L-3: the organiser sees reviews still pending (e.g. stranded after the grace).
    from admin_api.app.api.v1.routes_events import list_vendors
    ev, v, _, owner, item, _ = await _ended_vendor(db, bag, [_day(-2)])
    await _queued(db, owner, item, _sast(_day(-2), 9))
    items = (await list_vendors(ev["id"], ADMIN, db))["items"]
    [mine] = [i for i in items if i["vendor_id"] == v["vendor_id"]]
    assert mine["pending_offline_reviews"] == 1


async def test_online_checkout_cannot_reuse_a_key_awaiting_review(db, bag):
    # Review info-1: an online sale with a queued review's key is refused, the review is not stranded.
    biz, _, owner, item = await _restaurant(db, bag)
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    key, rid = await _queued(db, owner, item, _now() - timedelta(hours=80))
    online = POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                                   payment_method="CASH", cash_tendered_cents=10_000, idempotency_key=key)
    assert await _err(create_pos_order(online, owner, db)) == (409, "OFFLINE_REVIEW_KEY_CONFLICT")
    await db.rollback()
    assert await _orders(db, owner.business_id) == 0
    # an unrelated online key still works
    fresh = online.model_copy(update={"idempotency_key": f"online-{uuid.uuid4().hex}"})
    assert (await create_pos_order(fresh, owner, db)).payment_status == "PAID"


async def test_a_payload_that_no_longer_validates_is_a_422_not_a_500(db, bag):
    # Review info-2
    biz, _, owner, item = await _restaurant(db, bag)
    key, rid = await _queued(db, owner, item, _now() - timedelta(hours=80))
    await db.execute(update(OfflineCaptureReview).where(OfflineCaptureReview.id == rid).values(payload={"items": []}))
    await db.commit()
    assert await _err(approve_offline_review(rid, _note(), owner, db)) == (422, "OFFLINE_REVIEW_PAYLOAD_INVALID")
    [review] = await _reviews(db, owner.business_id)
    assert review.status == "PENDING"
