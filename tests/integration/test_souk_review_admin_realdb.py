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


# -- L-4 -----------------------------------------------------------------------

async def _code(coro):
    with pytest.raises((AppError, NotFoundError)) as exc:
        await coro
    return exc.value.status_code, exc.value.code


async def test_l4_admin_create_event_plan_business_never_has_whatsapp(db, bag):
    assert await _code(create_business(
        BusinessCreate(name="Ev Stall", plan="EVENT", whatsapp_phone_number_id=f"pn-{uuid.uuid4().hex}"),
        ADMIN, db)) == (422, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
    out = await create_business(BusinessCreate(name=f"Ev Stall {uuid.uuid4().hex[:5]}", plan="EVENT"), ADMIN, db)
    bag["extra_biz"].append(out.id)
    assert out.is_whatsapp_enabled is False
    biz = (await db.execute(select(Business).where(Business.id == out.id))).scalar_one()
    assert biz.is_whatsapp_enabled is False and biz.whatsapp_phone_number_id is None


async def test_l4_admin_patch_cannot_enable_whatsapp_or_give_a_number_to_an_event_business(db, bag):
    ev = await create_business(BusinessCreate(name=f"Ev {uuid.uuid4().hex[:5]}", plan="EVENT"), ADMIN, db)
    bag["extra_biz"].append(ev.id)
    assert await _code(update_business(ev.id, BusinessUpdate(is_whatsapp_enabled=True), ADMIN, db)) == (
        422, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
    assert await _code(update_business(
        ev.id, BusinessUpdate(whatsapp_phone_number_id=f"pn-{uuid.uuid4().hex}"), ADMIN, db)) == (
        422, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
    ok = await update_business(ev.id, BusinessUpdate(name="Renamed"), ADMIN, db)
    assert ok.name == "Renamed" and ok.is_whatsapp_enabled is False


async def test_l4_admin_patch_to_event_plan_forces_whatsapp_off_and_refuses_a_number(db, bag):
    number = f"pn-{uuid.uuid4().hex}"
    biz = await create_business(BusinessCreate(name=f"Rest {uuid.uuid4().hex[:5]}", plan="STARTER",
                                               whatsapp_phone_number_id=number), ADMIN, db)
    bag["extra_biz"].append(biz.id)
    await update_business(biz.id, BusinessUpdate(is_whatsapp_enabled=True), ADMIN, db)
    assert await _code(update_business(biz.id, BusinessUpdate(plan="EVENT"), ADMIN, db)) == (
        422, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
    assert (await db.execute(select(Business.plan).where(Business.id == biz.id))).scalar_one() == "STARTER"
    out = await update_business(
        biz.id, BusinessUpdate(plan="EVENT", whatsapp_phone_number_id=None), ADMIN, db)
    assert out.plan == "EVENT" and out.is_whatsapp_enabled is False and out.whatsapp_phone_number_id is None
    # enabling it afterwards is refused
    assert await _code(update_business(biz.id, BusinessUpdate(is_whatsapp_enabled=True), ADMIN, db)) == (
        422, "EVENT_PLAN_WHATSAPP_NOT_ALLOWED")
