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




# -- M-2 -----------------------------------------------------------------------

async def test_m2_public_registration_writes_only_the_new_vendors_rows(db, bag):
    ev_a, v1, _ = await _event_with_vendors(db, bag)
    # Make vendor A's stored state stale on purpose: a global reconcile would "repair" it.
    await db.execute(update(EventVendor).where(EventVendor.id == v1["vendor_id"]).values(status="PENDING"))
    await db.execute(update(Business).where(Business.id == v1["business_id"]).values(
        is_active=False, suspended_reason=EVENT_SUSPENSION_REASON))
    await db.commit()
    ev_b = await _open_event(db, bag)

    async def snapshot():
        v = (await db.execute(select(EventVendor.status, EventVendor.status_changed_at).where(
            EventVendor.id == v1["vendor_id"]).execution_options(populate_existing=True))).one()
        b = (await db.execute(select(Business.is_active, Business.suspended_reason, Business.updated_at).where(
            Business.id == v1["business_id"]).execution_options(populate_existing=True))).one()
        return tuple(v), tuple(b)

    before = await snapshot()
    reg = await _register(db, bag, ev_b)
    assert await snapshot() == before
    assert reg["business_id"] != v1["business_id"]


async def test_m2_new_registration_is_still_decided_for_its_own_business(db, bag):
    ev = await _open_event(db, bag)
    reg = await _register(db, bag, ev)
    biz = (await db.execute(select(Business).where(Business.id == reg["business_id"]))).scalar_one()
    vendor = (await db.execute(select(EventVendor).where(EventVendor.business_id == biz.id))).scalar_one()
    assert vendor.event_id == ev["id"]
    # active iff the decision grants access; otherwise suspended with the standard reason
    assert biz.is_active == (reg["access_level"] != "NONE")
    if not biz.is_active:
        assert biz.suspended_reason == EVENT_SUSPENSION_REASON


async def test_m2_per_event_registration_budget_is_configured_keyed_per_event_and_enforced(monkeypatch):
    assert str(pub.EVENT_REGISTER_PER_EVENT_LIMIT) == "30 per 1 hour"
    assert pub.event_register_budget_key(" Souk-A ") == "souk-a"

    class _FakeStrategy:
        def __init__(self):
            self.hits: dict[tuple, int] = {}

        def hit(self, item, *identifiers):
            self.hits[identifiers] = self.hits.get(identifiers, 0) + 1
            return self.hits[identifiers] <= item.amount

    monkeypatch.setattr(limiter, "enabled", True)
    monkeypatch.setattr(limiter, "_limiter", _FakeStrategy())
    for _ in range(30):
        pub._spend_event_register_budget("souk-a")
    with pytest.raises(AppError) as exc:
        pub._spend_event_register_budget("souk-a")
    assert exc.value.code == "EVENT_REGISTRATION_RATE_LIMITED" and exc.value.status_code == 429
    pub._spend_event_register_budget("souk-b")  # a different event has its own budget
    monkeypatch.setattr(limiter, "enabled", False)
    pub._spend_event_register_budget("souk-a")  # a disabled limiter never blocks


async def test_m2_budget_is_spent_only_after_the_registration_code_is_accepted(db, bag, monkeypatch):
    ev = await _open_event(db, bag)
    spent: list[str] = []
    monkeypatch.setattr(pub, "_spend_event_register_budget", lambda slug: spent.append(slug))
    with pytest.raises(AppError):
        await pub.register_vendor(ev["slug"], _body(bag, "WRONG-CODE"), None, db)
    assert spent == []
    await _register(db, bag, ev)
    assert spent == [ev["slug"]]


