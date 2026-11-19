"""
Real-Postgres tests for the souk security-review follow-ups (migration 0068):
H-1 cashier/approver role, H-2 event-vendor backdating, M-1 offline cash refund
and acknowledgement, M-2 scoped public registration, L-1..L-4.

Route functions are called directly with real sessions (same pattern as
test_offline_pos_sync_realdb.py).
"""
from __future__ import annotations

import inspect
import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text, update

from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    BusinessUpdate,
    create_business,
    update_business,
)
from admin_api.app.api.v1.routes_events import EventCreate, EventDayIn, create_event, list_vendors
from backend.app.api.v1 import routes_event_public as pub
from backend.app.api.v1 import routes_event_vendor as ven
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSPaymentIntentCreateRequest,
    POSRefundRequest,
    POSReturnLine,
    POSVoidRequest,
    TillCloseRequest,
    TillOpenRequest,
    acknowledge_offline_order,
    close_till,
    create_pos_order,
    list_flagged_offline_orders,
    open_till,
    refund_pos_order,
    void_pos_order,
)
from backend.app.api.v1.routes_variants import MatrixCreate, VariantCreate, VariantUpdate
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.ratelimit import limiter
from backend.app.core.rbac import require_owner
from backend.app.events.service import EVENT_SUSPENSION_REASON
from backend.app.events.trading import (
    EVENT_OFFLINE_CAPTURE_MAX_AGE,
    OFFLINE_LATE_SYNC_THRESHOLD,
    offline_capture_gate,
)
from backend.app.inventory.ledger import stock_on_hand
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.event import EventVendor
from shared.models.order import Order, OrderItem
from shared.models.variant import ProductVariant
from tests.integration.test_event_phase2_realdb import _body, _open_event, _register  # noqa: F401
from tests.integration.test_offline_pos_sync_realdb import (  # noqa: F401
    _ago,
    _biz_with_item,
    _err,
    _fetch,
    _item,
    _now,
    _req,
    _sast,
    _user_row,
)
from tests.integration.test_product_variants_realdb import _checkout, _matrix, _sell, _shop  # noqa: F401
from tests.integration.test_souk_security_review_realdb import (  # noqa: F401  (fixtures + helpers)
    ADMIN,
    DAYS,
    TODAY,
    _event,
    _pay,
    _row,
    _sub_id,
    _user,
    _vendors,
    bag,
)

pytestmark = pytest.mark.asyncio

INT32 = 2_147_483_647


async def _limit(db, biz, max_cents=100):
    biz.max_staff_discount_cents = max_cents  # via the ORM so the cached Business row sees it
    await db.commit()
    await db.refresh(biz)


async def _flagged(db, user, **kw):
    args = dict(reason=None, since=None, include_acknowledged=False, limit=50)
    args.update(kw)
    return await list_flagged_offline_orders(user=user, db=db, **args)


async def _backdate_till(db, biz_id, hours=2):
    await db.execute(text("update till_sessions set opened_at = now() - make_interval(hours => :h) "
                          "where business_id = :b and status = 'OPEN'"), {"h": hours, "b": biz_id})
    await db.commit()


# -- H-1 -----------------------------------------------------------------------

async def test_h1_staff_naming_the_owner_cannot_use_the_owners_discount_authority(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    await _limit(db, biz)
    owner_row = await _user_row(db, biz.id, role="OWNER", name="The Owner")
    staff = _user(biz.id, "STAFF")
    code = await _err(create_pos_order(
        _req(item.id, _ago(minutes=5), captured_by_user_id=owner_row.id, discount_cents=5000), staff, db))
    assert code == (403, "DISCOUNT_LIMIT_EXCEEDED")
    assert (await db.execute(select(func.count(Order.id)).where(Order.business_id == biz.id))).scalar_one() == 0


async def test_h1_manager_naming_the_owner_is_limited_to_manager_rules(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    await _limit(db, biz)
    owner_row = await _user_row(db, biz.id, role="OWNER", name="The Owner")
    manager = _user(biz.id, "MANAGER")
    code = await _err(create_pos_order(
        _req(item.id, _ago(minutes=5), captured_by_user_id=owner_row.id, discount_cents=5000), manager, db))
    assert code == (403, "DISCOUNT_LIMIT_EXCEEDED")
    ok = await create_pos_order(  # within the limit it is fine
        _req(item.id, _ago(minutes=5), captured_by_user_id=owner_row.id, discount_cents=100), manager, db)
    assert ok.discount_cents == 100


async def test_h1_owner_naming_a_staff_cashier_is_limited_to_the_cashiers_role(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    await _limit(db, biz)
    staff_row = await _user_row(db, biz.id, role="STAFF")
    code = await _err(create_pos_order(
        _req(item.id, _ago(minutes=5), captured_by_user_id=staff_row.id, discount_cents=5000), owner, db))
    assert code == (403, "DISCOUNT_LIMIT_EXCEEDED")
    ok = await create_pos_order(_req(item.id, _ago(minutes=5), discount_cents=5000), owner, db)  # owner as cashier
    assert ok.discount_cents == 5000


async def test_h1_discount_approver_is_always_the_syncing_caller(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    await _limit(db, biz, 1000)
    staff_row = await _user_row(db, biz.id, role="STAFF")
    manager = _user(biz.id, "MANAGER")
    out = await create_pos_order(
        _req(item.id, _ago(minutes=5), captured_by_user_id=staff_row.id, discount_cents=500), manager, db)
    row = await _fetch(db, out.id)
    assert row.created_by_user_id == staff_row.id               # who rang it up
    assert row.discount_approved_by_user_id == manager.user_id  # who answers for the discount
    own = await create_pos_order(_req(item.id, _ago(minutes=4), discount_cents=500), owner, db)
    assert (await _fetch(db, own.id)).discount_approved_by_user_id == owner.user_id
    nodisc = await create_pos_order(_req(item.id, _ago(minutes=3)), owner, db)
    assert (await _fetch(db, nodisc.id)).discount_approved_by_user_id is None


async def test_h1_sale_for_a_different_cashier_is_flagged_with_a_reason_and_listed(db, bag):
    biz, owner, item = await _biz_with_item(db, bag)
    staff_row = await _user_row(db, biz.id, role="STAFF")
    other = await create_pos_order(_req(item.id, _ago(minutes=5), captured_by_user_id=staff_row.id), owner, db)
    assert "cashier_differs_from_syncer" in (await _fetch(db, other.id)).offline_review_reasons
    own = await create_pos_order(_req(item.id, _ago(minutes=4)), owner, db)
    assert "cashier_differs_from_syncer" not in (await _fetch(db, own.id)).offline_review_reasons
    listed = await _flagged(db, owner, reason="cashier_differs_from_syncer")
    assert [o.id for o in listed] == [other.id]
    assert "cashier_differs_from_syncer" in listed[0].review_reasons


async def test_h1_cashier_flag_alone_makes_a_sale_reviewable(db, bag):
    biz, owner, item = await _shop(db, bag)  # till open
    await _backdate_till(db, biz.id)
    staff_row = await _user_row(db, biz.id, role="STAFF")
    out = await create_pos_order(_req(item.id, _ago(minutes=10), captured_by_user_id=staff_row.id), owner, db)
    row = await _fetch(db, out.id)
    assert not row.offline_unattributed and not row.offline_stock_shortfall
    assert row.offline_review_required and row.offline_review_reasons == ["cashier_differs_from_syncer"]


# -- H-2 -----------------------------------------------------------------------

async def _event_with_vendors(db, bag):
    """An event spanning yesterday..today+4 with two vendors, only the first paid, and only for yesterday."""
    yesterday = TODAY - timedelta(days=1)
    ev = await create_event(EventCreate(name=f"H2 Souk {uuid.uuid4().hex[:6]}",
                                        days=[EventDayIn(day=d) for d in [yesterday] + DAYS]), ADMIN, db)
    bag["events"].append(ev["id"])
    v1, v2 = await _vendors(db, ev, bag, [_row(bag, starts=yesterday, ends=yesterday),
                                          _row(bag, starts=yesterday, ends=yesterday)])
    await _pay(db, ev, v1)
    return ev, v1, v2


async def test_h2_backdating_into_a_paid_day_from_an_unpaid_day_is_flagged_and_visible_to_the_organiser(db, bag):
    ev, v1, v2 = await _event_with_vendors(db, bag)
    owner = _user(v1["business_id"])
    item = await _item(db, owner)
    out = await create_pos_order(_req(item.id, _sast(TODAY - timedelta(days=1), 20)), owner, db)
    row = await _fetch(db, out.id)
    assert row.offline_review_required
    assert "capture_day_unpaid_at_sync" in row.offline_review_reasons
    listed = await _flagged(db, owner, reason="capture_day_unpaid_at_sync")
    assert [o.id for o in listed] == [out.id]
    items = {v["business_id"]: v for v in (await list_vendors(ev["id"], ADMIN, db))["items"]}
    assert items[v1["business_id"]]["flagged_offline_orders"] == 1
    assert items[v2["business_id"]]["flagged_offline_orders"] == 0
    # An owner's acknowledgement clears it from the organiser's count too.
    await acknowledge_offline_order(out.id, owner, db)
    items = {v["business_id"]: v for v in (await list_vendors(ev["id"], ADMIN, db))["items"]}
    assert items[v1["business_id"]]["flagged_offline_orders"] == 0


async def test_h2_event_vendor_capture_older_than_36h_is_refused_but_72h_still_applies_elsewhere(db, bag):
    assert EVENT_OFFLINE_CAPTURE_MAX_AGE == timedelta(hours=36)
    ev, v1, _ = await _event_with_vendors(db, bag)
    owner = _user(v1["business_id"])
    item = await _item(db, owner)
    with pytest.raises(AppError) as exc:
        await create_pos_order(_req(item.id, _ago(hours=40)), owner, db)
    assert exc.value.code == "OFFLINE_CAPTURE_TOO_OLD" and exc.value.details == {"max_age_hours": 36}
    # a non-event business keeps 72h
    biz, powner, pitem = await _biz_with_item(db, bag)
    ok = await create_pos_order(_req(pitem.id, _ago(hours=60)), powner, db)
    assert ok.is_offline_capture
    assert await _err(create_pos_order(_req(pitem.id, _ago(hours=80)), powner, db)) == (
        422, "OFFLINE_CAPTURE_TOO_OLD")


async def test_h2_late_sync_flag_applies_to_any_plan_beyond_12h(db, bag):
    assert OFFLINE_LATE_SYNC_THRESHOLD == timedelta(hours=12)
    biz, owner, item = await _biz_with_item(db, bag)
    late = await create_pos_order(_req(item.id, _ago(hours=13)), owner, db)
    fresh = await create_pos_order(_req(item.id, _ago(hours=11)), owner, db)
    assert "late_sync" in (await _fetch(db, late.id)).offline_review_reasons
    assert "late_sync" not in (await _fetch(db, fresh.id)).offline_review_reasons
    assert {o.id for o in await _flagged(db, owner, reason="late_sync")} == {late.id}
    gate = await offline_capture_gate(db, biz.id, _ago(hours=13))
    assert gate.reasons == ["late_sync"]


# -- M-1 -----------------------------------------------------------------------

async def _unattributed_cash_sale(db, bag):
    biz, owner, item = await _shop(db, bag)  # till opened just now: a capture 10h ago is before it
    out = await create_pos_order(_req(item.id, _ago(hours=10)), owner, db)
    row = await _fetch(db, out.id)
    assert row.offline_unattributed and row.till_session_id is None
    return biz, owner, item, out


async def test_m1_manager_cannot_refund_or_void_an_unattributed_offline_cash_sale_but_owner_can(db, bag):
    biz, owner, item, out = await _unattributed_cash_sale(db, bag)
    manager = _user(biz.id, "MANAGER")
    assert await _err(refund_pos_order(out.id, POSRefundRequest(reason="x"), manager, db)) == (
        403, "OFFLINE_REFUND_REQUIRES_OWNER")
    assert await _err(void_pos_order(out.id, POSVoidRequest(reason="x"), manager, db)) == (
        403, "OFFLINE_REFUND_REQUIRES_OWNER")
    assert (await _fetch(db, out.id)).payment_status == "PAID"  # nothing moved
    refunded = await refund_pos_order(out.id, POSRefundRequest(reason="owner reviewed"), owner, db)
    assert refunded.payment_status == "REFUNDED"


async def test_m1_manager_can_still_refund_a_normally_attributed_offline_sale(db, bag):
    biz, owner, item = await _shop(db, bag)
    await _backdate_till(db, biz.id)
    out = await create_pos_order(_req(item.id, _ago(minutes=30)), owner, db)
    assert not (await _fetch(db, out.id)).offline_unattributed
    done = await refund_pos_order(out.id, POSRefundRequest(reason="ok"), _user(biz.id, "MANAGER"), db)
    assert done.payment_status == "REFUNDED"


async def _open_session_id(db, biz_id):
    return (await db.execute(text("select id from till_sessions where business_id = :b and status = 'OPEN'"),
                             {"b": biz_id})).scalar_one()


async def test_m1_till_close_counts_every_unreviewed_unattributed_sale_until_an_owner_acknowledges(db, bag):
    biz, owner, item = await _shop(db, bag)
    sale = await create_pos_order(_req(item.id, _ago(hours=10)), owner, db)   # before any session
    assert (await _fetch(db, sale.id)).offline_unattributed
    c1 = await close_till(await _open_session_id(db, biz.id), TillCloseRequest(counted_cash_cents=0), owner, db)
    assert c1.offline_unattributed_count == 1 and c1.offline_unattributed_cash_cents == 10000
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    c2 = await close_till(await _open_session_id(db, biz.id), TillCloseRequest(counted_cash_cents=0), owner, db)
    assert c2.offline_unattributed_count == 1 and c2.offline_unattributed_cash_cents == 10000  # still counted
    await acknowledge_offline_order(sale.id, owner, db)
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
    c3 = await close_till(await _open_session_id(db, biz.id), TillCloseRequest(counted_cash_cents=0), owner, db)
    assert c3.offline_unattributed_count == 0 and c3.offline_unattributed_cash_cents == 0


async def test_m1_acknowledge_is_owner_only_audited_idempotent_and_tenant_scoped(db, bag):
    dep = inspect.signature(acknowledge_offline_order).parameters["user"].default.dependency
    assert dep is require_owner  # a manager is refused by the RBAC dependency

    biz, owner, item = await _biz_with_item(db, bag)
    out = await create_pos_order(_req(item.id, _ago(minutes=5)), owner, db)   # no till: flagged
    assert [o.id for o in await _flagged(db, owner)] == [out.id]
    done = await acknowledge_offline_order(out.id, owner, db)
    assert done.offline_acknowledged_by_user_id == owner.user_id and done.offline_acknowledged_at is not None
    assert await _flagged(db, owner) == []
    assert [o.id for o in await _flagged(db, owner, include_acknowledged=True)] == [out.id]
    first_at = (await _fetch(db, out.id)).offline_acknowledged_at
    again = await acknowledge_offline_order(out.id, owner, db)
    assert again.offline_acknowledged_at == first_at
    audits = (await db.execute(select(AuditEvent).where(
        AuditEvent.action == "order.pos.offline_acknowledged", AuditEvent.target_id == out.id))).scalars().all()
    assert len(audits) == 1 and audits[0].actor_user_id == owner.user_id and audits[0].business_id == biz.id

    other, other_owner, other_item = await _biz_with_item(db, bag)
    assert (await _err(acknowledge_offline_order(out.id, other_owner, db)))[0] == 404  # another tenant's order
    assert (await _fetch(db, out.id)).offline_acknowledged_by_user_id == owner.user_id


# -- L-1 (POS)

async def test_l1_integer_overflow_is_a_422_not_a_500():
    item = uuid.uuid4()
    base = dict(items=[POSOrderItemRequest(menu_item_id=item, quantity=1)], payment_method="CASH",
                cash_tendered_cents=100)
    for bad in (dict(offline_captured_at=_now(), idempotency_key="k", offline_client_total_cents=INT32 + 1),
                dict(discount_cents=INT32 + 1), dict(cash_tendered_cents=INT32 + 1)):
        with pytest.raises(ValidationError):
            POSOrderCreateRequest(**{**base, **bad})
    ok = POSOrderCreateRequest(**base, offline_captured_at=_now(), idempotency_key="k",
                               offline_client_total_cents=INT32)
    assert ok.offline_client_total_cents == INT32
    with pytest.raises(ValidationError):
        TillOpenRequest(opening_float_cents=INT32 + 1)
    with pytest.raises(ValidationError):
        TillCloseRequest(counted_cash_cents=INT32 + 1)
    with pytest.raises(ValidationError):
        POSRefundRequest(amount_cents=INT32 + 1)
    with pytest.raises(ValidationError):
        POSRefundRequest(cash_refund_cents=INT32 + 1)
    with pytest.raises(ValidationError):
        POSPaymentIntentCreateRequest(amount_cents=INT32 + 1)


async def test_l1_an_order_whose_lines_sum_past_the_integer_column_is_a_422(db, bag):
    biz, owner, item = await _shop(db, bag)
    v = (await _matrix(db, owner, item, sizes=["M"], colours=["Black"], price_cents=2_000_000_000,
                       cost_cents=None, opening_stock=None, track_stock=False))["LS-M-BLACK"]
    code = await _err(_checkout(db, owner, [_sell(v.id, 2, item.id)]))
    assert code == (422, "ORDER_TOTAL_TOO_LARGE")


# -- L-2 -----------------------------------------------------------------------

async def test_l2_claim_reference_is_audited_and_shared_references_are_flagged_to_the_organiser(db, bag):
    ev = await _event(db, bag)
    v1, v2, v3 = await _vendors(db, ev, bag, [_row(bag, starts=DAYS[0]), _row(bag, starts=DAYS[0]),
                                              _row(bag, starts=DAYS[0])])
    subs = {}
    for v, ref in ((v1, "EFT-9"), (v2, " eft-9 "), (v3, "EFT-UNIQUE")):
        sid = await _sub_id(db, ev["id"], v["vendor_id"])
        subs[v["vendor_id"]] = sid
        await ven.claim_payment(sid, ven.PaymentClaim(reference=ref), _user(v["business_id"]), db)
    audit = (await db.execute(select(AuditEvent).where(
        AuditEvent.action == "event.subscription_payment_claimed",
        AuditEvent.target_id == subs[v1["vendor_id"]]))).scalar_one()
    assert audit.diff_json["payment_claim_reference"] == "EFT-9"
    items = {v["vendor_id"]: v for v in (await list_vendors(ev["id"], ADMIN, db))["items"]}

    def shared(v):
        return items[v["vendor_id"]]["subscriptions"][0]["payment_claim_shared"]

    assert shared(v1) is True and shared(v2) is True and shared(v3) is False


# -- L-3 -----------------------------------------------------------------------

async def test_l3_partial_return_restocks_the_ingredient_actually_consumed(db, bag):
    biz, owner, item = await _shop(db, bag, price=5000)
    vs = await _matrix(db, owner, item, sizes=["M", "L"], colours=["Black"])
    m, l = vs["LS-M-BLACK"], vs["LS-L-BLACK"]
    ing = {r[0]: r[1] for r in (await db.execute(select(ProductVariant.id, ProductVariant.ingredient_id).where(
        ProductVariant.id.in_([m.id, l.id])))).all()}
    sale = await _checkout(db, owner, [_sell(m.id, 2, item.id)])
    line = sale.items[0]
    stored = (await db.execute(
        select(OrderItem.stock_ingredient_id_snapshot).where(OrderItem.id == line.id))).scalar_one()
    assert stored == ing[m.id]
    # An admin re-links the M variant to L's ingredient after the sale.
    await db.execute(update(ProductVariant).where(ProductVariant.id == m.id).values(ingredient_id=ing[l.id]))
    await db.commit()
    before = await stock_on_hand(db, biz.id, ingredient_ids=[ing[m.id], ing[l.id]])
    await refund_pos_order(sale.id, POSRefundRequest(
        reason="one back", restore_stock=True, return_items=[POSReturnLine(order_item_id=line.id, quantity=1)]),
        owner, db)
    after = await stock_on_hand(db, biz.id, ingredient_ids=[ing[m.id], ing[l.id]])
    assert after[ing[m.id]] == before[ing[m.id]] + 1000      # back to the ingredient that was consumed
    assert after[ing[l.id]] == before[ing[l.id]]             # not onto the re-linked one


