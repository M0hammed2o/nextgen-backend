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


# -- L-1 (variants) -----------------------------------------------------------------------

async def test_l1_variant_and_matrix_fields_are_bounded():
    attrs = {"size": "M"}
    for kw in (dict(price_cents=INT32 + 1), dict(cost_cents=INT32 + 1), dict(sort_order=INT32 + 1),
               dict(opening_stock=1_000_001)):
        with pytest.raises(ValidationError):
            VariantCreate(sku="A", attributes=attrs, **kw)
    with pytest.raises(ValidationError):  # opening value (units x cost) overflows the INTEGER ledger column
        VariantCreate(sku="A", attributes=attrs, cost_cents=INT32, opening_stock=2)
    for kw in (dict(price_cents=INT32 + 1), dict(cost_cents=INT32 + 1), dict(opening_stock=1_000_001)):
        with pytest.raises(ValidationError):
            MatrixCreate(sku_prefix="P", sizes=["S"], **kw)
    with pytest.raises(ValidationError):
        MatrixCreate(sku_prefix="P", sizes=["S"], cost_cents=INT32, opening_stock=2)
    for kw in (dict(price_cents=INT32 + 1), dict(cost_cents=INT32 + 1), dict(sort_order=INT32 + 1)):
        with pytest.raises(ValidationError):
            VariantUpdate(**kw)
    VariantCreate(sku="A", attributes=attrs, price_cents=INT32, cost_cents=INT32, sort_order=INT32)


