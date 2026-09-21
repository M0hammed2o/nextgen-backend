"""
Usage-tracking commit (Wave 0A / BUG-0050).

`increment_usage` (backend/app/bot/usage_tracker.py) only flushes, by
design — it never commits, so a caller mid-transaction isn't forced to
commit early. Both order-creation routes used to call it AFTER their only
commit, so the increment was flushed into a session that then closed
without ever committing it: every POS and manual-dashboard order was
silently never counted toward usage/quota tracking.

These tests prove the fix directly: after a successful order creation, a
second `db.commit()` happens specifically to persist the usage increment,
and it is scoped inside the same try/except a tracking failure was already
protected by (so a broken usage tracker still can never fail the sale).
"""
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from backend.app.core.rbac import AuthUser


def make_user(role="STAFF", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


# ── POS checkout (routes_pos.py::create_pos_order) ──────────────────────────

class TestPOSUsageCommit:
    def _setup(self):
        from tests.test_pos_orders import FakeDB, FakeResult, make_business, make_resolved_item

        return FakeDB, FakeResult, make_business, make_resolved_item

    async def test_usage_increment_is_committed_after_order_commit(self):
        FakeDB, FakeResult, make_business, make_resolved_item = self._setup()
        from backend.app.api.v1 import routes_pos
        from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
        from shared.models.till import TillSession

        user = make_user()
        business = make_business(timezone="Africa/Johannesburg")
        till = TillSession(
            id=uuid.uuid4(), business_id=user.business_id, opened_by_user_id=uuid.uuid4(),
            opening_float_cents=0, status="OPEN", opened_at=datetime.now(timezone.utc),
        )
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CASH", cash_tendered_cents=5000,
        )
        db = FakeDB([FakeResult(scalar=till), FakeResult(scalar=business), FakeResult(row=(1, "ZAR"))])

        with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()) as mock_increment:
                    order = await create_pos_order(body=body, user=user, db=db)

        # 1 commit for the sale itself + 1 commit for the usage increment.
        assert db.commits == 2
        mock_increment.assert_awaited_once()
        _, args, kwargs = mock_increment.mock_calls[0]
        assert args[1] == user.business_id
        assert args[2] == "Africa/Johannesburg"  # business's own timezone, not the old hardcoded default
        assert kwargs.get("orders_created") == 1 or args[3] == 1

    async def test_usage_tracker_failure_never_fails_the_sale(self):
        """A broken usage tracker must not turn a successful checkout into a 500."""
        FakeDB, FakeResult, make_business, make_resolved_item = self._setup()
        from backend.app.api.v1 import routes_pos
        from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
        from shared.models.till import TillSession

        user = make_user()
        business = make_business(timezone="Africa/Johannesburg")
        till = TillSession(
            id=uuid.uuid4(), business_id=user.business_id, opened_by_user_id=uuid.uuid4(),
            opening_float_cents=0, status="OPEN", opened_at=datetime.now(timezone.utc),
        )
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CASH", cash_tendered_cents=5000,
        )
        db = FakeDB([FakeResult(scalar=till), FakeResult(scalar=business), FakeResult(row=(1, "ZAR"))])

        with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch(
                    "backend.app.bot.usage_tracker.increment_usage",
                    AsyncMock(side_effect=RuntimeError("usage tracker exploded")),
                ):
                    order = await create_pos_order(body=body, user=user, db=db)

        # The sale itself still succeeded — only its own commit happened.
        assert order.id is not None
        assert db.commits == 1

    async def test_falls_back_to_default_timezone_when_business_has_none(self):
        FakeDB, FakeResult, make_business, make_resolved_item = self._setup()
        from backend.app.api.v1 import routes_pos
        from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
        from shared.models.till import TillSession

        user = make_user()
        business = make_business(timezone=None)
        till = TillSession(
            id=uuid.uuid4(), business_id=user.business_id, opened_by_user_id=uuid.uuid4(),
            opening_float_cents=0, status="OPEN", opened_at=datetime.now(timezone.utc),
        )
        body = POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
            payment_method="CASH", cash_tendered_cents=5000,
        )
        db = FakeDB([FakeResult(scalar=till), FakeResult(scalar=business), FakeResult(row=(1, "ZAR"))])

        with patch.object(routes_pos, "resolve_pos_items", AsyncMock(return_value=[make_resolved_item()])):
            with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
                with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()) as mock_increment:
                    await create_pos_order(body=body, user=user, db=db)

        _, args, _kwargs = mock_increment.mock_calls[0]
        assert args[2] == "Africa/Johannesburg"


# ── Manual/dashboard order (routes_orders.py::create_order) ─────────────────

class FakeResultOrders:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._row


class FakeDBOrders:
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
        # Simulate the column-level defaults SQLAlchemy's real ORM unit-of-
        # work would apply at flush time against a real Session — this fake
        # never runs that machinery, so anything with a model-level
        # `default=` has to be filled in by hand for OrderResponse to
        # validate against a never-really-flushed object.
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
            obj.updated_at = now
        if hasattr(obj, "payment_status") and getattr(obj, "payment_status", None) is None:
            obj.payment_status = "PENDING"
        if hasattr(obj, "payment_required") and getattr(obj, "payment_required", None) is None:
            obj.payment_required = False
        if hasattr(obj, "discount_cents") and getattr(obj, "discount_cents", None) is None:
            obj.discount_cents = 0
        if hasattr(obj, "tax_cents") and getattr(obj, "tax_cents", None) is None:
            obj.tax_cents = 0

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


class TestManualOrderUsageCommit:
    async def test_manual_order_usage_increment_is_committed(self):
        from backend.app.api.v1.routes_orders import OrderCreate, OrderItemCreate, create_order

        user = make_user(role="OWNER")
        body = OrderCreate(items=[OrderItemCreate(name="Burger", qty=1, price_cents=5000)])

        db = FakeDBOrders([
            FakeResultOrders(row=(1, "ZAR", 0)),   # order-number-sequence increment
            FakeResultOrders(scalar="Africa/Johannesburg"),  # business timezone lookup
        ])

        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            with patch("backend.app.bot.usage_tracker.increment_usage", AsyncMock()) as mock_increment:
                order = await create_order(body=body, user=user, db=db)

        assert order.id is not None
        assert db.commits == 2  # order commit + usage commit
        mock_increment.assert_awaited_once()
        _, args, _kwargs = mock_increment.mock_calls[0]
        assert args[2] == "Africa/Johannesburg"

    async def test_manual_order_usage_tracker_failure_never_fails_order(self):
        from backend.app.api.v1.routes_orders import OrderCreate, OrderItemCreate, create_order

        user = make_user(role="OWNER")
        body = OrderCreate(items=[OrderItemCreate(name="Burger", qty=1, price_cents=5000)])

        db = FakeDBOrders([
            FakeResultOrders(row=(1, "ZAR", 0)),
            FakeResultOrders(scalar="Africa/Johannesburg"),
        ])

        with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=Exception("no redis in tests"))):
            with patch(
                "backend.app.bot.usage_tracker.increment_usage",
                AsyncMock(side_effect=RuntimeError("boom")),
            ):
                order = await create_order(body=body, user=user, db=db)

        assert order.id is not None
        assert db.commits == 1
