"""
POS order creation route (backend/app/api/v1/routes_pos.py::create_pos_order).

Menu/option/add-on resolution itself is covered in test_pos_pricing.py; these
tests patch resolve_pos_items out and focus on what create_pos_order alone
owns: the discount-limit gate (staff need owner sign-off above the business's
configured limit; owner is never limited), the discount-vs-subtotal guard,
and the business-scope audit trail for discounts.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_pos
from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.pos.pricing import ResolvedPOSItem
from shared.enums import OrderSource
from shared.pricing.engine import LineItemBreakdown


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeResult:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        # Simulate the id/created_at/updated_at defaults SQLAlchemy would
        # normally apply at INSERT time, so response schemas can validate.
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_resolved_item(line_total_cents=5000):
    breakdown = LineItemBreakdown(
        base_price_cents=line_total_cents,
        option_adjustment_cents=0,
        add_on_total_cents=0,
        unit_price_cents=line_total_cents,
        quantity=1,
        line_total_cents=line_total_cents,
    )
    return ResolvedPOSItem(
        menu_item_id=uuid.uuid4(),
        name="Cheeseburger",
        breakdown=breakdown,
        selected_options_snapshot=[],
        add_ons_snapshot=[],
        special_instructions=None,
    )


def make_request(discount_cents=0, line_total_cents=5000):
    return POSOrderCreateRequest(
        items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
        discount_cents=discount_cents,
    ), line_total_cents


async def _run(body, user, max_staff_discount_cents=0, resolved_items=None):
    """Runs create_pos_order with resolve_pos_items patched out."""
    resolved_items = resolved_items or [make_resolved_item()]
    results = [
        FakeResult(scalar=max_staff_discount_cents),                  # Business.max_staff_discount_cents
        FakeResult(row=(1, "ZAR", 0)),                                # order_number_sequence, currency, delivery_fee
    ]
    db = FakeDB(results)
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            order = await create_pos_order(body=body, user=user, db=db)
    return order, db


# ── Discount limit enforcement ──────────────────────────────────────────────

async def test_staff_discount_within_limit_succeeds():
    user = make_user(role="STAFF")
    body, _ = make_request(discount_cents=500)
    order, db = await _run(body, user, max_staff_discount_cents=1000)

    assert order.source == OrderSource.POS.value
    assert db.commits == 1


async def test_staff_discount_above_limit_is_blocked():
    user = make_user(role="STAFF")
    body, _ = make_request(discount_cents=1500)

    with pytest.raises(AppError) as exc:
        await _run(body, user, max_staff_discount_cents=1000)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"
    assert exc.value.status_code == 403


async def test_manager_discount_above_limit_is_also_blocked():
    """Only OWNER bypasses the limit — MANAGER is subject to it, same as STAFF."""
    user = make_user(role="MANAGER")
    body, _ = make_request(discount_cents=1500)

    with pytest.raises(AppError) as exc:
        await _run(body, user, max_staff_discount_cents=1000)
    assert exc.value.code == "DISCOUNT_LIMIT_EXCEEDED"


async def test_owner_discount_above_limit_is_allowed():
    user = make_user(role="OWNER")
    body, _ = make_request(discount_cents=1500)
    order, db = await _run(body, user, max_staff_discount_cents=1000)

    assert order.discount_cents == 1500
    assert db.commits == 1


async def test_discount_exceeding_subtotal_is_rejected_before_any_db_write():
    user = make_user(role="OWNER")
    body, line_total = make_request(discount_cents=9999, line_total_cents=5000)

    resolved_items = [make_resolved_item(line_total_cents=line_total)]
    db = FakeDB([])  # no execute() call should happen — proves the guard runs first
    with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=resolved_items)):
        with pytest.raises(AppError) as exc:
            await create_pos_order(body=body, user=user, db=db)

    assert exc.value.code == "DISCOUNT_EXCEEDS_SUBTOTAL"
    assert db.added == []
    assert db.commits == 0


# ── Audit trail ──────────────────────────────────────────────────────────────

async def test_discount_writes_a_business_scope_audit_event():
    user = make_user(role="OWNER")
    body, _ = make_request(discount_cents=500)
    order, db = await _run(body, user, max_staff_discount_cents=0)

    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].scope == "BUSINESS"
    assert audit_events[0].action == "order.pos.discount_applied"
    assert audit_events[0].diff_json["discount_cents"] == 500


async def test_no_discount_writes_no_audit_event():
    user = make_user(role="STAFF")
    body, _ = make_request(discount_cents=0)
    order, db = await _run(body, user, max_staff_discount_cents=0)

    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert audit_events == []
