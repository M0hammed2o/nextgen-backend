"""
Delivery reconciliation entitlement (migration 0054,
Business.delivery_reconciliation_enabled) — server-side enforcement on
routes_external_orders.py's CSV/manual reconciliation endpoints.

Previously this entitlement was frontend-visibility only (deliberately, per
migration 0054's own docstring: zero production tenants relied on CSV
reconciliation at the time, so there was nothing to retroactively cut off).
A real-production check since then confirmed that is still true, so the
backend is now the authoritative gate, same as every other commercial
module in this codebase (inventory, loyalty, advanced analytics, the live
delivery-marketplace integrations).

Confirms, per route:
  - `delivery_reconciliation_enabled=False` -> 409
    DELIVERY_RECONCILIATION_NOT_ENABLED, no mutation, no downstream service
    call;
  - `delivery_reconciliation_enabled=True` -> the gate does not block, and
    the route reaches its real logic (proven for preview/commit by mocking
    only the CSV-parsing/service layer, not the entitlement check itself);
  - the two delivery entitlements are genuinely independent --
    `delivery_platforms_enabled` alone (the LIVE marketplace integrations,
    enforced separately in routes_delivery.py) never satisfies this gate,
    and `delivery_reconciliation_enabled` alone always does, regardless of
    `delivery_platforms_enabled`'s value;
  - one business's entitlement never leaks into another's call.

Every "false" test passes `file=None`/a bare body rather than real upload
content: the gate fires immediately after `_business()`, before anything
in the function ever touches `file`, which is exactly the "no mutation"
property being proven -- if the gate's position in the function ever
regressed to after the first read/write, these tests would start raising
AttributeError on `None.filename` etc. instead of AppError, and fail loudly
rather than silently passing.
"""
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.v1.routes_external_orders import (
    MappingCreateRequest,
    commit_external_orders,
    delete_mapping,
    external_sales_status,
    list_batches,
    list_mappings,
    preview_external_orders,
    upsert_mapping,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.external_orders.adapters import AdapterResult
from backend.app.external_orders.service import ImportOutcome, ResolutionSummary


class FakeResult:
    def __init__(self, scalar=None, scalars_all=None):
        self._scalar = scalar
        self._scalars_all = scalars_all if scalars_all is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_all

        class _Scalars:
            def all(self):
                return items

        return _Scalars()


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    async def delete(self, obj):
        self.added = [o for o in self.added if o is not obj]


def make_user(business_id=None) -> AuthUser:
    return AuthUser(
        user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(),
        role="OWNER", token_type="access",
    )


def make_business(business_id, **overrides):
    biz = types.SimpleNamespace(
        id=business_id,
        delivery_reconciliation_enabled=False,
        delivery_platforms_enabled=False,
        inventory_enabled=False,
    )
    for k, v in overrides.items():
        setattr(biz, k, v)
    return biz


# ── False path: every guarded route refuses, with no mutation ──────────────

class TestDisabledRefusesEveryRoute:
    async def test_preview_refused(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await preview_external_orders(file=None, platform="UBER_EATS", mapping=None, user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409
        assert "not enabled" in exc.value.message.lower()
        assert db.commits == 0

    async def test_commit_refused_and_creates_no_batch(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await commit_external_orders(
                file=None, platform="UBER_EATS", mapping=None,
                include_cancelled=False, acknowledge_closed_period=False,
                user=user, db=db,
            )
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409
        assert db.commits == 0
        assert db.added == []  # no ExternalImportBatch, no AuditEvent

    async def test_status_refused(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await external_sales_status(user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409

    async def test_batches_refused(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await list_batches(limit=30, user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409

    async def test_list_mappings_refused(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await list_mappings(platform=None, user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409

    async def test_upsert_mapping_refused_and_creates_nothing(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        body = MappingCreateRequest(platform="UBER_EATS", kind="ITEM", external_text="Pizza", is_ignored=True)
        with pytest.raises(AppError) as exc:
            await upsert_mapping(body=body, user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409
        assert db.commits == 0
        assert db.added == []

    async def test_delete_mapping_refused(self):
        user = make_user()
        db = FakeDB([FakeResult(scalar=make_business(user.business_id))])
        with pytest.raises(AppError) as exc:
            await delete_mapping(mapping_id=uuid.uuid4(), user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
        assert exc.value.status_code == 409
        assert db.commits == 0


# ── True path: the gate does not block, real logic is reached ──────────────

class TestEnabledWorkflowRemainsFunctional:
    async def test_preview_proceeds_to_real_resolution(self):
        user = make_user()
        business = make_business(user.business_id, delivery_reconciliation_enabled=True, inventory_enabled=True)
        db = FakeDB([FakeResult(scalar=business)])

        with patch(
            "backend.app.api.v1.routes_external_orders._read_rows", new=AsyncMock(return_value=[{"x": "1"}])
        ) as mock_read, patch(
            "backend.app.api.v1.routes_external_orders.adapt_rows", return_value=AdapterResult()
        ) as mock_adapt, patch(
            "backend.app.api.v1.routes_external_orders.resolve_orders",
            new=AsyncMock(return_value=ResolutionSummary()),
        ) as mock_resolve:
            result = await preview_external_orders(
                file=MagicMock(), platform="UBER_EATS", mapping=None, user=user, db=db
            )

        # The gate let it through all the way to the real pipeline -- not
        # just "didn't raise", but actually invoked the CSV-parsing/
        # resolution service functions this route exists to call.
        mock_read.assert_awaited_once()
        mock_adapt.assert_called_once()
        mock_resolve.assert_awaited_once()
        assert result["can_import"] is False  # nothing in an empty summary to import
        assert result["inventory_enabled"] is True

    async def test_commit_proceeds_and_creates_a_batch(self):
        user = make_user()
        business = make_business(user.business_id, delivery_reconciliation_enabled=True)
        db = FakeDB([FakeResult(scalar=business)])

        with patch(
            "backend.app.api.v1.routes_external_orders._read_rows", new=AsyncMock(return_value=[])
        ), patch(
            "backend.app.api.v1.routes_external_orders.adapt_rows", return_value=AdapterResult()
        ), patch(
            "backend.app.api.v1.routes_external_orders.resolve_orders",
            new=AsyncMock(return_value=ResolutionSummary()),
        ), patch(
            "backend.app.api.v1.routes_external_orders.import_resolved_orders",
            new=AsyncMock(return_value=ImportOutcome()),
        ) as mock_import:
            result = await commit_external_orders(
                file=MagicMock(filename="report.csv"), platform="UBER_EATS", mapping=None,
                include_cancelled=False, acknowledge_closed_period=False,
                user=user, db=db,
            )

        mock_import.assert_awaited_once()
        assert db.commits == 1
        assert result["orders_imported"] == 0
        # A real ExternalImportBatch (and its AuditEvent) were actually
        # created, not just "no error raised".
        assert len(db.added) == 2


# ── The two delivery entitlements are genuinely independent ────────────────

class TestEntitlementsDoNotSubstituteForEachOther:
    async def test_platforms_alone_does_not_authorize_csv_reconciliation(self):
        """Item 5: delivery_platforms_enabled=True, delivery_reconciliation_enabled=False
        MUST still refuse -- the live-marketplace entitlement is never a
        substitute for the CSV-reconciliation one."""
        user = make_user()
        business = make_business(
            user.business_id, delivery_platforms_enabled=True, delivery_reconciliation_enabled=False,
        )
        db = FakeDB([FakeResult(scalar=business)])
        with pytest.raises(AppError) as exc:
            await list_batches(limit=30, user=user, db=db)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"

    async def test_reconciliation_alone_authorizes_csv_reconciliation(self):
        """Item 6: delivery_reconciliation_enabled=True, delivery_platforms_enabled=False
        MUST allow CSV/manual reconciliation -- the live-marketplace
        entitlement is not a prerequisite."""
        user = make_user()
        business = make_business(
            user.business_id, delivery_reconciliation_enabled=True, delivery_platforms_enabled=False,
        )
        db = FakeDB([FakeResult(scalar=business), FakeResult(scalars_all=[])])
        result = await list_batches(limit=30, user=user, db=db)
        assert result == []


# ── Tenant isolation: one business's entitlement never leaks to another ────

class TestTenantIsolation:
    async def test_one_enabled_business_does_not_enable_a_different_business(self):
        """Item 4: business_id always comes from the authenticated user
        (AuthUser.business_id), never a request parameter, and each call's
        entitlement check reads that exact business's own row -- entitled
        Business A's state must never leak into disentitled Business B's
        call, even when both calls happen in the same test process."""
        user_a = make_user()
        user_b = make_user()
        assert user_a.business_id != user_b.business_id

        business_a = make_business(user_a.business_id, delivery_reconciliation_enabled=True)
        business_b = make_business(user_b.business_id, delivery_reconciliation_enabled=False)

        db_a = FakeDB([FakeResult(scalar=business_a), FakeResult(scalars_all=[])])
        result_a = await list_batches(limit=30, user=user_a, db=db_a)
        assert result_a == []

        db_b = FakeDB([FakeResult(scalar=business_b)])
        with pytest.raises(AppError) as exc:
            await list_batches(limit=30, user=user_b, db=db_b)
        assert exc.value.code == "DELIVERY_RECONCILIATION_NOT_ENABLED"
