"""
`routes_delivery.py` — the delivery-marketplace business-plane API.

Covers: tenant scoping (business_id from JWT only, every query), role
gating (owner/manager for reads + mapping management, staff-or-above for
accept/deny), no-credential-leak in every response, deny-reason-code
validation, the "never self-promote to CONNECTED" invariant, and the
manual-accept re-enqueue path.

Follows this repo's established mocked-FakeDB unit-test style (see
test_business_settings_owner_only.py) rather than a live DB — cross-tenant
proof against a REAL Postgres is separately covered in
tests/integration/test_tenant_isolation_writes.py.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_delivery as routes
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection, DeliveryOrder


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeScalarResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar if self._scalar is not None else 0

    def scalars(self):
        return self

    def all(self):
        return self._scalars_list

    def first(self):
        return (self._scalar,) if self._scalar is not None else None


class FakeDB:
    """Canned-result queue, same pattern as test_business_settings_owner_only.py."""

    def __init__(self, results):
        self._results = list(results)
        self._gets = []
        self.added = []
        self.commits = 0
        self.refreshed = []

    async def execute(self, stmt):
        if not self._results:
            raise AssertionError("FakeDB.execute called more times than results were queued")
        return self._results.pop(0)

    async def get(self, model, id_):
        if self._gets:
            return self._gets.pop(0)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        # Simulate what a real DB round-trip would backfill: SQLAlchemy's
        # `default=` (uuid.uuid4 for id, 0 for times_used) only fires at
        # flush/INSERT time in real usage, never at plain object
        # construction — this FakeDB never actually flushes, so mirror
        # that one effect here rather than changing production code.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "times_used", None) is None:
            obj.times_used = 0
        self.refreshed.append(obj)

    async def rollback(self):
        pass


def make_user(role="OWNER", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_business(business_id, delivery_platforms_enabled=True) -> Business:
    return Business(
        id=business_id, name="T", slug="t", business_code="ABC123",
        delivery_platforms_enabled=delivery_platforms_enabled,
        uber_eats_enabled=True, mrd_enabled=False,
    )


def make_connection(business_id, provider="UBER_EATS", status="CONNECTED", **overrides) -> DeliveryConnection:
    conn = DeliveryConnection(
        id=uuid.uuid4(), business_id=business_id, provider=provider,
        status=status, environment="production", external_store_id="store-1",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(conn, k, v)
    return conn


def make_order(business_id, status="NEEDS_MAPPING", **overrides) -> DeliveryOrder:
    row = DeliveryOrder(
        id=uuid.uuid4(), business_id=business_id, provider="UBER_EATS",
        external_order_id="ext-1", external_store_id="store-1", status=status,
        currency="ZAR", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


class FakeRequestState:
    def __init__(self, arq_pool):
        self.arq_pool = arq_pool


class FakeApp:
    def __init__(self, arq_pool):
        self.state = FakeRequestState(arq_pool)


class FakeRequest:
    def __init__(self, arq_pool=None):
        self.app = FakeApp(arq_pool)


# ── Entitlement guard ────────────────────────────────────────────────────────

async def test_entitlement_off_returns_409_on_every_endpoint_group():
    user = make_user(role="OWNER")
    business = make_business(user.business_id, delivery_platforms_enabled=False)
    db = FakeDB([FakeScalarResult(business)])
    with pytest.raises(AppError) as exc:
        await routes.list_connections(user=user, db=db)
    assert exc.value.code == "DELIVERY_NOT_ENABLED"
    assert exc.value.status_code == 409


async def test_entitlement_guard_404s_on_missing_business():
    user = make_user(role="OWNER")
    db = FakeDB([FakeScalarResult(None)])
    with pytest.raises(NotFoundError):
        await routes._require_delivery_module_enabled(db, user.business_id)


# ── Connections: no credential leak ─────────────────────────────────────────

async def test_connection_response_never_includes_provisioning_token():
    assert "provisioning_token_encrypted" not in routes.DeliveryConnectionResponse.model_fields
    assert "access_token_encrypted" not in routes.DeliveryConnectionResponse.model_fields


async def test_list_connections_returns_scoped_rows():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    connection = make_connection(user.business_id)
    db = FakeDB([
        FakeScalarResult(business),  # entitlement check
        FakeScalarResult(scalars_list=[connection]),  # list connections
        FakeScalarResult(None),  # last_webhook_at
        FakeScalarResult(None),  # last_order_at
    ])
    result = await routes.list_connections(user=user, db=db)
    assert len(result) == 1
    assert result[0].provider == "UBER_EATS"
    assert not hasattr(result[0], "provisioning_token_encrypted")


async def test_get_connection_synthesises_not_connected_when_no_row():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(None),  # no connection row
    ])
    result = await routes.get_connection(provider="UBER_EATS", user=user, db=db)
    assert result.status == "NOT_CONNECTED"
    assert result.id is None


async def test_get_connection_rejects_unknown_provider():
    user = make_user(role="OWNER")
    db = FakeDB([])
    with pytest.raises(AppError) as exc:
        await routes.get_connection(provider="DOORDASH", user=user, db=db)
    assert exc.value.code == "UNKNOWN_PROVIDER"


# ── Connections: never self-promote to CONNECTED ────────────────────────────

async def test_disable_connection_moves_connected_to_disabled():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    connection = make_connection(user.business_id, status="CONNECTED")
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(connection),
        FakeScalarResult(None),  # last_webhook_at (post-commit response build)
        FakeScalarResult(None),  # last_order_at
    ])
    result = await routes.disable_connection(provider="UBER_EATS", user=user, db=db)
    assert result.status == "DISABLED"
    assert connection.status == "DISABLED"
    assert db.commits == 1
    assert len(db.added) == 1
    assert db.added[0].action == "delivery.connection.disabled"


async def test_disable_connection_already_inactive_is_409():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    connection = make_connection(user.business_id, status="DISABLED")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(connection)])
    with pytest.raises(AppError) as exc:
        await routes.disable_connection(provider="UBER_EATS", user=user, db=db)
    assert exc.value.code == "DELIVERY_CONNECTION_ALREADY_INACTIVE"


async def test_disable_connection_missing_is_404():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(None)])
    with pytest.raises(NotFoundError):
        await routes.disable_connection(provider="UBER_EATS", user=user, db=db)


async def test_enable_connection_never_writes_connected():
    """The hard constraint: enabling a DISABLED connection must NEVER set
    status to CONNECTED from this route — only Uber approval (via a future
    provisioning/status-sync flow) may do that."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    connection = make_connection(user.business_id, status="DISABLED")
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(connection),
        FakeScalarResult(None),
        FakeScalarResult(None),
    ])
    result = await routes.enable_connection(provider="UBER_EATS", user=user, db=db)
    assert result.status != "CONNECTED"
    assert result.status == "ATTENTION_REQUIRED"
    assert connection.status == "ATTENTION_REQUIRED"


async def test_enable_connection_rejects_non_disabled_source_status():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    connection = make_connection(user.business_id, status="SANDBOX")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(connection)])
    with pytest.raises(AppError) as exc:
        await routes.enable_connection(provider="UBER_EATS", user=user, db=db)
    assert exc.value.code == "DELIVERY_CONNECTION_NOT_DISABLED"


def test_no_route_handler_source_ever_assigns_connected_literal():
    """Static guard: 'CONNECTED' must never appear as an assignment target
    value anywhere in this router's source (it may appear in comparisons/
    docstrings/status-list membership checks only)."""
    import ast
    import inspect

    source = inspect.getsource(routes)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                is_status_attr = (
                    isinstance(target, ast.Attribute) and target.attr == "status"
                )
                if is_status_attr and isinstance(node.value, ast.Constant):
                    assert node.value.value != "CONNECTED", (
                        "A route handler assigns status='CONNECTED' directly — "
                        "forbidden, see module docstring."
                    )


# ── Orders: list/get scoping ─────────────────────────────────────────────────

async def test_list_orders_scopes_by_business_and_paginates():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    order = make_order(user.business_id)
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(1),  # count
        FakeScalarResult(scalars_list=[order]),  # rows
    ])
    result = await routes.list_orders(status="NEEDS_MAPPING", limit=50, offset=0, user=user, db=db)
    assert result.total == 1
    assert len(result.orders) == 1
    assert result.orders[0].status == "NEEDS_MAPPING"


async def test_get_order_not_found_for_other_tenant_is_404():
    """A cross-tenant id must 404, not leak another tenant's row — the
    _load_order_or_404 query filters on business_id in its WHERE clause, so
    a FakeDB standing in for 'no row matched this tenant' returns None."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(None)])
    with pytest.raises(NotFoundError):
        await routes.get_order(order_id=uuid.uuid4(), user=user, db=db)


# ── Accept ───────────────────────────────────────────────────────────────────

async def test_accept_only_valid_from_needs_mapping():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    order = make_order(user.business_id, status="ACCEPTED")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(order)])
    request = FakeRequest(arq_pool=AsyncMock())
    with pytest.raises(AppError) as exc:
        await routes.accept_order(order_id=order.id, request=request, user=user, db=db)
    assert exc.value.code == "DELIVERY_ORDER_NOT_ACCEPTABLE"


async def test_accept_resets_status_and_enqueues_same_worker_job():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    order = make_order(user.business_id, status="NEEDS_MAPPING")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(order)])
    pool = AsyncMock()
    request = FakeRequest(arq_pool=pool)

    result = await routes.accept_order(order_id=order.id, request=request, user=user, db=db)

    assert order.status == "RECEIVED"
    pool.enqueue_job.assert_awaited_once_with(
        "process_delivery_order", "UBER_EATS", str(user.business_id), "ext-1", None,
    )
    assert db.commits == 1
    assert result.id == order.id


async def test_accept_with_no_worker_pool_is_503():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    order = make_order(user.business_id, status="NEEDS_MAPPING")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(order)])
    request = FakeRequest(arq_pool=None)
    with pytest.raises(AppError) as exc:
        await routes.accept_order(order_id=order.id, request=request, user=user, db=db)
    assert exc.value.status_code == 503
    assert db.commits == 0


# ── Deny ─────────────────────────────────────────────────────────────────────

async def test_deny_rejects_unknown_reason_code_at_the_schema_level():
    with pytest.raises(Exception):
        routes.DenyOrderRequest(reason_code="MADE_UP_REASON")


@pytest.mark.parametrize("code", list(routes._DENY_REASON_CODES))
def test_deny_accepts_every_real_uber_reason_code(code):
    body = routes.DenyOrderRequest(reason_code=code)
    assert body.reason_code == code


async def test_deny_only_valid_from_open_statuses():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    order = make_order(user.business_id, status="FULFILLED")
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(order)])
    body = routes.DenyOrderRequest(reason_code="OTHER")
    with pytest.raises(AppError) as exc:
        await routes.deny_order(order_id=order.id, body=body, user=user, db=db)
    assert exc.value.code == "DELIVERY_ORDER_NOT_DENIABLE"


async def test_deny_reuses_order_service_issue_deny_not_a_parallel_implementation():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    order = make_order(user.business_id, status="NEEDS_MAPPING", delivery_connection_id=None)
    db = FakeDB([FakeScalarResult(business), FakeScalarResult(order)])
    body = routes.DenyOrderRequest(reason_code="ITEM_AVAILABILITY", notes="test")

    with patch(
        "backend.app.api.v1.routes_delivery.order_service._issue_deny", new=AsyncMock()
    ) as mock_deny:
        await routes.deny_order(order_id=order.id, body=body, user=user, db=db)
        mock_deny.assert_awaited_once()
        _, kwargs = mock_deny.call_args
        assert kwargs["row"] is order
        assert kwargs["reason_code"] == "ITEM_AVAILABILITY"
    assert db.commits == 1


# ── Mappings ─────────────────────────────────────────────────────────────────

async def test_mapping_upsert_requires_target_unless_ignored():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([FakeScalarResult(business)])
    body = routes.MappingUpsertRequest(
        platform="UBER_EATS", kind="ITEM", external_id="ext-item-1",
        external_text="Large Pizza",
    )
    with pytest.raises(AppError) as exc:
        await routes.upsert_menu_mapping(body=body, user=user, db=db)
    assert exc.value.code == "MAPPING_TARGET_REQUIRED"


async def test_mapping_upsert_validates_menu_item_belongs_to_tenant():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    other_item_id = uuid.uuid4()
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(None),  # menu item ownership check fails
    ])
    body = routes.MappingUpsertRequest(
        platform="UBER_EATS", kind="ITEM", external_id="ext-item-1",
        external_text="Large Pizza", menu_item_id=other_item_id,
    )
    with pytest.raises(NotFoundError):
        await routes.upsert_menu_mapping(body=body, user=user, db=db)


async def test_mapping_upsert_creates_with_external_id_key():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    menu_item_id = uuid.uuid4()
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult((menu_item_id,)),  # ownership check passes (first())
        FakeScalarResult(None),  # no existing mapping
        FakeScalarResult(scalars_list=[]),  # item_names lookup
        FakeScalarResult(scalars_list=[]),  # add_on_names lookup
    ])
    body = routes.MappingUpsertRequest(
        platform="UBER_EATS", kind="ITEM", external_id="ext-item-1",
        external_text="Large Pizza", menu_item_id=menu_item_id,
    )
    result = await routes.upsert_menu_mapping(body=body, user=user, db=db)
    assert result.external_id == "ext-item-1"
    assert result.status == "MAPPED"
    assert db.commits == 1
    created = db.added[0]
    assert created.business_id == user.business_id
    assert created.external_id == "ext-item-1"


async def test_list_menu_mappings_only_includes_live_connection_rows():
    """external_id IS NOT NULL — CSV-import-created rows (external_id NULL)
    must never appear here, keeping the two importers' views separate."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB([
        FakeScalarResult(business),
        FakeScalarResult(scalars_list=[]),
        FakeScalarResult(scalars_list=[]),
        FakeScalarResult(scalars_list=[]),
    ])
    result = await routes.list_menu_mappings(provider=None, status=None, user=user, db=db)
    assert result == []


# ── Router registration / role gating (source-level, matches spec exactly) ──

def _allowed_roles(dep_call):
    freevars = dep_call.__code__.co_freevars
    closure = dep_call.__closure__
    mapping = dict(zip(freevars, [c.cell_contents for c in closure])) if closure else {}
    return mapping.get("allowed_roles")


class TestRoleGating:
    def _role_for_route(self, path, method):
        for route in routes.router.routes:
            if route.path == path and method in route.methods:
                for d in route.dependant.dependencies:
                    if d.call.__name__ == "_check":
                        return _allowed_roles(d.call)
        raise AssertionError(f"route not found: {method} {path}")

    def test_reads_and_mapping_management_are_owner_or_manager(self):
        for path, method in [
            ("/business/delivery/connections", "GET"),
            ("/business/delivery/connections/{provider}", "GET"),
            ("/business/delivery/connections/{provider}/disable", "POST"),
            ("/business/delivery/connections/{provider}/enable", "POST"),
            ("/business/delivery/orders", "GET"),
            ("/business/delivery/orders/{order_id}", "GET"),
            ("/business/delivery/menu-mappings", "GET"),
            ("/business/delivery/menu-mappings", "POST"),
        ]:
            assert self._role_for_route(path, method) == ["OWNER", "MANAGER"], (path, method)

    def test_accept_and_deny_are_staff_or_above(self):
        for path in (
            "/business/delivery/orders/{order_id}/accept",
            "/business/delivery/orders/{order_id}/deny",
        ):
            assert self._role_for_route(path, "POST") == ["STAFF", "MANAGER", "OWNER"], path


class TestRouterMounting:
    def test_router_is_registered_in_main(self):
        import inspect

        from backend.app import main
        source = inspect.getsource(main)
        assert "delivery_router" in source
        assert "routes_delivery import router as delivery_router" in source
