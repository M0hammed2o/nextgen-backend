"""
Marketplace completion pass (2026-10-26) — API surface and Uber HTTP layer:
the staff delivery queue, unconfirmed-accept resolution, store pause/resume,
catalogue import, health, role gating, the kitchen->provider enqueue in
routes_orders, SUPER_ADMIN connection management, per-environment
credentials/hosts/tokens, the CSV order-total and live-API dedupe rules,
webhook retention, and the production boot guard.
"""
import types
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.api.v1 import routes_delivery as routes
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from backend.app.delivery.base import DeliveryOrderContext
from backend.app.delivery.canonical import CanonicalCatalogueEntry
from backend.app.delivery.errors import DeliveryCapabilityUnsupported, DeliveryProviderError
from shared.models.delivery import DeliveryOrder
from shared.models.external_orders import ExternalProductMapping
from shared.models.menu import MenuAddOn, MenuItem
from tests.test_routes_delivery import (
    FakeDB,
    FakeScalarResult,
    make_business,
    make_connection,
    make_order,
    make_user,
)


class _Rows:
    """Result for `.all()` on a multi-column select (health aggregates)."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


# ── Response contract ────────────────────────────────────────────────────────

def test_unresolved_items_are_objects_with_an_id_and_legacy_strings_survive():
    business_id = uuid.uuid4()
    new = make_order(business_id, unresolved_items_json=[
        {"kind": "ITEM", "external_id": "u-1", "external_text": "Mystery"},
    ])
    legacy = make_order(business_id, unresolved_items_json=["Old Name"])
    assert routes.DeliveryOrderResponse.from_row(new).unresolved_items == [
        {"kind": "ITEM", "external_id": "u-1", "external_text": "Mystery"},
    ]
    assert routes.DeliveryOrderResponse.from_row(legacy).unresolved_items == [
        {"kind": None, "external_id": None, "external_text": "Old Name"},
    ]


# ── Staff queue ──────────────────────────────────────────────────────────────

async def test_staff_queue_shows_what_was_ordered_and_what_staff_may_do():
    user = make_user(role="STAFF")
    row = make_order(
        user.business_id, status="NEEDS_MAPPING", display_id="AB123",
        accept_deadline_at=datetime.now(timezone.utc) + timedelta(minutes=8),
        gross_cents=12345, fee_cents=500,
        unresolved_items_json=[{"kind": "ITEM", "external_id": "u-9", "external_text": "Secret Burger"}],
        fetched_order_snapshot_json={
            "customer_first_name": "Larry", "special_instructions": "Ring",
            "lines": [{"title": "Secret Burger", "quantity": 2, "modifiers": [{"title": "Onion", "removed": True}]}],
        },
    )
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(scalars_list=[row])])
    queue = await routes.staff_queue(user=user, db=db)
    assert len(queue) == 1
    item = queue[0]
    assert item.display_id == "AB123"
    assert item.waiting_for_manager is True and item.can_accept is False and item.can_deny is True
    assert item.unresolved == ["Secret Burger"]
    assert item.lines[0]["modifiers"][0] == {"title": "Onion", "quantity": None, "removed": True}
    dumped = item.model_dump()
    assert "gross_cents" not in dumped and "fee_cents" not in dumped, "staff view carries no payout figures"


async def test_staff_queue_requires_the_delivery_module():
    user = make_user(role="STAFF")
    db = FakeDB([FakeScalarResult(make_business(user.business_id, delivery_platforms_enabled=False))])
    with pytest.raises(AppError) as exc:
        await routes.staff_queue(user=user, db=db)
    assert exc.value.code == "DELIVERY_NOT_ENABLED"


def test_queue_item_permissions_by_status():
    business_id = uuid.uuid4()
    ready = routes.DeliveryQueueItem.from_row(make_order(business_id, status="READY_TO_ACCEPT"))
    unconfirmed = routes.DeliveryQueueItem.from_row(make_order(business_id, status="ACCEPT_UNCONFIRMED"))
    assert (ready.can_accept, ready.can_retry_accept) == (True, False)
    assert (unconfirmed.can_accept, unconfirmed.can_deny, unconfirmed.can_retry_accept) == (False, False, True)


# ── Unconfirmed accepts ──────────────────────────────────────────────────────

async def test_retry_accept_success_moves_to_accepted_with_the_staff_member_as_actor():
    user = make_user(role="STAFF")
    row = make_order(user.business_id, status="ACCEPT_UNCONFIRMED", order_id=uuid.uuid4(),
                     delivery_connection_id=None, failure_reason="no answer")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(row)])
    adapter = MagicMock(accept_order=AsyncMock())
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        result = await routes.retry_accept(order_id=row.id, user=user, db=db)
    assert result.status == "ACCEPTED" and row.failure_reason is None
    audit = db.added[-1]
    assert audit.action == "delivery.order.retry_accept_succeeded" and audit.actor_user_id == user.user_id


async def test_retry_accept_refused_by_provider_is_a_409_and_stays_unconfirmed():
    user = make_user(role="STAFF")
    row = make_order(user.business_id, status="ACCEPT_UNCONFIRMED", order_id=uuid.uuid4(), delivery_connection_id=None)
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(row)])
    adapter = MagicMock(accept_order=AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "gone", 404)))
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        with pytest.raises(AppError) as exc:
            await routes.retry_accept(order_id=row.id, user=user, db=db)
    assert exc.value.code == "DELIVERY_ACCEPT_REFUSED"
    assert row.status == "ACCEPT_UNCONFIRMED" and "Retry accept failed" in row.last_provider_sync_error


async def test_retry_accept_only_for_unconfirmed_orders():
    user = make_user(role="STAFF")
    row = make_order(user.business_id, status="ACCEPTED")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(row)])
    with pytest.raises(AppError) as exc:
        await routes.retry_accept(order_id=row.id, user=user, db=db)
    assert exc.value.code == "DELIVERY_ORDER_NOT_UNCONFIRMED"


async def test_manager_can_mark_an_unconfirmed_accept_as_accepted_and_it_is_audited():
    user = make_user(role="MANAGER")
    row = make_order(user.business_id, status="ACCEPT_UNCONFIRMED")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(row)])
    result = await routes.mark_accepted(order_id=row.id, user=user, db=db)
    assert result.status == "ACCEPTED"
    assert db.added[-1].action == "delivery.order.marked_accepted_by_manager"


# ── Store pause / resume ─────────────────────────────────────────────────────

async def test_pause_store_calls_the_provider_and_audits():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, status="CONNECTED", store_status="ONLINE")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(connection)])
    adapter = MagicMock(set_store_status=AsyncMock())
    until = datetime(2026, 10, 26, 22, 0, tzinfo=timezone.utc)
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        result = await routes.set_store_status(
            provider="UBER_EATS", body=routes.StoreStatusRequest(status="PAUSED", paused_until=until, reason="busy"),
            user=user, db=db,
        )
    kwargs = adapter.set_store_status.call_args.kwargs
    assert kwargs["status"] == "PAUSED" and kwargs["paused_until"] == until.isoformat()
    assert result.store_status == "PAUSED"
    assert db.added[-1].action == "delivery.connection.store_status_changed"


async def test_provider_without_the_capability_gets_an_honest_409_not_a_silent_no_op():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, provider="MR_D", status="ATTENTION_REQUIRED")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(connection)])
    adapter = MagicMock(set_store_status=AsyncMock(side_effect=DeliveryCapabilityUnsupported("MR_D", "store_pause_resume")))
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        with pytest.raises(AppError) as exc:
            await routes.set_store_status(
                provider="MR_D", body=routes.StoreStatusRequest(status="PAUSED"), user=user, db=db,
            )
    assert exc.value.code == "DELIVERY_CAPABILITY_UNSUPPORTED"


async def test_store_status_refuses_an_inactive_connection():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, status="DISABLED")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(connection)])
    with pytest.raises(AppError) as exc:
        await routes.get_store_status(provider="UBER_EATS", user=user, db=db)
    assert exc.value.code == "DELIVERY_CONNECTION_INACTIVE"


# ── Catalogue import ─────────────────────────────────────────────────────────

async def test_catalogue_import_auto_maps_exact_names_adopts_csv_rows_and_flags_renames():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, status="AWAITING_APPROVAL")
    margherita = MenuItem(id=uuid.uuid4(), business_id=user.business_id, name="Margherita", price_cents=9000)
    cheese = MenuAddOn(id=uuid.uuid4(), business_id=user.business_id, name="Extra Cheese", price_cents=1500)
    csv_row = ExternalProductMapping(
        id=uuid.uuid4(), business_id=user.business_id, platform="UBER_EATS", kind="ITEM",
        external_text="Garlic Bread", external_key="garlic bread", external_id=None,
        menu_item_id=uuid.uuid4(), status="MAPPED",
    )
    renamed = ExternalProductMapping(
        id=uuid.uuid4(), business_id=user.business_id, platform="UBER_EATS", kind="ITEM",
        external_text="Pepperoni", external_key="pepperoni", external_id="u-pep",
        menu_item_id=uuid.uuid4(), status="MAPPED", provider_snapshot_json={"title": "Pepperoni"},
    )
    db = FakeDB([
        FakeScalarResult(make_business(user.business_id)),
        FakeScalarResult(connection),
        FakeScalarResult(scalars_list=[csv_row, renamed]),
        FakeScalarResult(scalars_list=[margherita]),
        FakeScalarResult(scalars_list=[cheese]),
    ])
    entries = [
        CanonicalCatalogueEntry(kind="ITEM", external_id="u-marg", title="Margherita", price_cents=11000),
        CanonicalCatalogueEntry(kind="ITEM", external_id="u-garlic", title="Garlic Bread", price_cents=4500),
        CanonicalCatalogueEntry(kind="ITEM", external_id="u-pep", title="Spicy Pepperoni", price_cents=12000),
        CanonicalCatalogueEntry(kind="ITEM", external_id="u-mystery", title="Chef Special", price_cents=15000),
        CanonicalCatalogueEntry(kind="ITEM", external_id="u-marg2", title="Margherita", price_cents=13000),
        CanonicalCatalogueEntry(kind="MODIFIER", external_id="u-cheese", title="Extra Cheese", price_cents=1000,
                                group_title="Extras"),
    ]
    adapter = MagicMock(fetch_menu_catalogue=AsyncMock(return_value=entries))
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        result = await routes.import_provider_catalogue(
            body=routes.CatalogueImportRequest(provider="UBER_EATS"), user=user, db=db,
        )
    assert (result.fetched, result.created, result.auto_mapped) == (6, 4, 2)
    assert (result.adopted_from_csv, result.renamed, result.name_collisions) == (1, 1, 1)

    created = {m.external_id: m for m in db.added if isinstance(m, ExternalProductMapping)}
    assert created["u-marg"].status == "MAPPED" and created["u-marg"].menu_item_id == margherita.id
    assert created["u-cheese"].menu_add_on_id == cheese.id
    assert created["u-mystery"].status == "UNMAPPED", "nothing fuzzier than an exact name"
    assert created["u-marg2"].status == "UNMAPPED" and created["u-marg2"].external_key != "margherita"
    assert created["u-marg"].provider_snapshot_json["price_cents"] == 11000, "channel price kept"
    assert csv_row.external_id == "u-garlic", "the CSV mapping is adopted, never duplicated"
    assert renamed.status == "CHANGED" and "Spicy Pepperoni" in renamed.status_reason
    assert db.added[-1].action == "delivery.menu_mappings.catalogue_imported"


async def test_catalogue_import_surfaces_provider_failure_without_writing():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, status="SANDBOX")
    db = FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(connection)])
    adapter = MagicMock(fetch_menu_catalogue=AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "x", 500)))
    with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
        with pytest.raises(AppError) as exc:
            await routes.import_provider_catalogue(
                body=routes.CatalogueImportRequest(provider="UBER_EATS"), user=user, db=db,
            )
    assert exc.value.status_code == 503 and db.commits == 0 and db.added == []


def test_mr_d_catalogue_import_is_refused_at_the_schema():
    with pytest.raises(Exception):
        routes.CatalogueImportRequest(provider="MR_D")


# ── Health ───────────────────────────────────────────────────────────────────

async def test_health_reports_acceptance_rate_and_open_work():
    user = make_user(role="OWNER")
    connection = make_connection(user.business_id, status="CONNECTED")
    db = FakeDB([
        FakeScalarResult(make_business(user.business_id)),
        FakeScalarResult(scalars_list=[connection]),
        _Rows([("UBER_EATS", "NEEDS_MAPPING", 1), ("UBER_EATS", "ACCEPT_UNCONFIRMED", 1)]),
        _Rows([("UBER_EATS", "ACCEPTED", 97), ("UBER_EATS", "FULFILLED", 1), ("UBER_EATS", "DENIED", 2)]),
        _Rows([("UBER_EATS", 3)]),
        _Rows([("UBER_EATS", 5)]),
        _Rows([("UBER_EATS", datetime(2026, 10, 26, tzinfo=timezone.utc))]),
    ])
    health = await routes.delivery_health(user=user, db=db)
    h = health[0]
    assert (h.accepted_24h, h.denied_24h, h.acceptance_rate_24h) == (98, 2, 0.98)
    assert (h.open_orders, h.needs_mapping, h.accept_unconfirmed) == (2, 1, 1)
    assert h.needs_attention is True, "an unconfirmed accept needs a human"
    assert (h.provider_sync_errors, h.unmapped_menu_entries) == (3, 5)


# ── Role gating for the new routes ───────────────────────────────────────────

def _roles(path, method):
    from tests.test_routes_delivery import _allowed_roles
    for route in routes.router.routes:
        if route.path == path and method in route.methods:
            for d in route.dependant.dependencies:
                if d.call.__name__ == "_check":
                    return _allowed_roles(d.call)
    raise AssertionError(f"route not found: {method} {path}")


@pytest.mark.parametrize("path,method", [
    ("/business/delivery/orders/{order_id}/mark-accepted", "POST"),
    ("/business/delivery/connections/{provider}/store-status", "GET"),
    ("/business/delivery/connections/{provider}/store-status", "POST"),
    ("/business/delivery/menu-mappings/import", "POST"),
    ("/business/delivery/health", "GET"),
])
def test_management_routes_are_owner_or_manager(path, method):
    assert _roles(path, method) == ["OWNER", "MANAGER"]


@pytest.mark.parametrize("path,method", [
    ("/business/delivery/queue", "GET"),
    ("/business/delivery/orders/{order_id}/retry-accept", "POST"),
])
def test_staff_routes_are_staff_or_above(path, method):
    assert _roles(path, method) == ["STAFF", "MANAGER", "OWNER"]


def test_owner_manager_order_list_was_not_widened_to_staff():
    assert _roles("/business/delivery/orders", "GET") == ["OWNER", "MANAGER"]


# ── routes_orders: kitchen status change -> provider push is queued ─────────

class _OrdersDB:
    def __init__(self, order):
        self.order = order
        self.added = []

    async def execute(self, stmt):
        return types.SimpleNamespace(scalar_one_or_none=lambda: self.order)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


@pytest.mark.parametrize("source,expect_enqueue", [("UBER_EATS", True), ("POS", False), ("WHATSAPP", False)])
async def test_status_change_enqueues_provider_sync_only_for_marketplace_orders(source, expect_enqueue):
    from backend.app.api.v1.routes_orders import StatusUpdateRequest, update_order_status
    from shared.models.order import Order

    user = make_user(role="STAFF")
    order = Order(
        id=uuid.uuid4(), business_id=user.business_id, order_number="UE-AB123", status="IN_PROGRESS",
        source=source, payment_required=False, payment_status="PAID", total_cents=1000,
        order_mode="DELIVERY", customer_id=None, items=[],
    )
    pool = MagicMock(enqueue_job=AsyncMock())
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(arq_pool=pool)))
    with patch("backend.app.api.v1.routes_orders.OrderResponse.model_validate", return_value=MagicMock()):
        await update_order_status(order.id, StatusUpdateRequest(status="READY"), user=user,
                                  db=_OrdersDB(order), request=request)
    if expect_enqueue:
        pool.enqueue_job.assert_awaited_once_with(
            "sync_delivery_order_status", str(user.business_id), str(order.id), "READY", None,
        )
    else:
        pool.enqueue_job.assert_not_awaited()


# ── Admin: SUPER_ADMIN connection management ────────────────────────────────

class _AdminDB:
    def __init__(self, business, existing=None):
        self.business = business
        self.existing = existing
        self.added = []
        self.commits = 0

    async def get(self, model, id_):
        return self.business

    async def execute(self, stmt):
        return types.SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    async def rollback(self):
        pass


def _admin():
    from backend.app.core.rbac import AuthUser
    return AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


def _biz(**flags):
    values = dict(delivery_platforms_enabled=True, uber_eats_enabled=True, mrd_enabled=False)
    values.update(flags)
    return types.SimpleNamespace(id=uuid.uuid4(), **values)


async def _admin_put(provider, body, *, business=None, existing=None):
    from admin_api.app.api.v1 import routes_delivery_connections as admin_routes
    business = business or _biz()
    db = _AdminDB(business, existing)
    result = await admin_routes.upsert_delivery_connection(
        business_id=business.id, provider=provider,
        body=admin_routes.AdminDeliveryConnectionUpsert(**body), user=_admin(), db=db,
    )
    return result, db


async def test_admin_registers_a_sandbox_store_and_audits_it():
    result, db = await _admin_put("UBER_EATS", {"status": "SANDBOX", "environment": "sandbox", "external_store_id": "test-store-1"})
    assert (result.status, result.environment, result.external_store_id) == ("SANDBOX", "sandbox", "test-store-1")
    audit = db.added[-1]
    assert audit.action == "admin.delivery_connection.upserted" and audit.scope == "PLATFORM"
    assert audit.target_id == result.id


@pytest.mark.parametrize("body,code", [
    ({"status": "SANDBOX", "environment": "production", "external_store_id": "s"}, "DELIVERY_STATUS_ENV_MISMATCH"),
    ({"status": "CONNECTED", "environment": "sandbox", "external_store_id": "s"}, "DELIVERY_STATUS_ENV_MISMATCH"),
    ({"status": "AWAITING_APPROVAL", "environment": "production"}, "DELIVERY_STORE_ID_REQUIRED"),
    ({"status": "CONNECTED", "environment": "production", "external_store_id": "s"}, "DELIVERY_APPROVAL_REFERENCE_REQUIRED"),
])
async def test_admin_status_rules(body, code):
    with pytest.raises(AppError) as exc:
        await _admin_put("UBER_EATS", body)
    assert exc.value.code == code


async def test_admin_connected_needs_entitlements_and_production_credentials():
    body = {"status": "CONNECTED", "environment": "production", "external_store_id": "s",
            "uber_approval_reference": "UBER-APPROVAL-123"}
    with pytest.raises(AppError) as exc:
        await _admin_put("UBER_EATS", body, business=_biz(uber_eats_enabled=False))
    assert exc.value.code == "DELIVERY_ENTITLEMENT_OFF"

    with patch.multiple(get_settings(), UBER_EATS_CLIENT_ID="", UBER_EATS_CLIENT_SECRET=""):
        with pytest.raises(AppError) as exc:
            await _admin_put("UBER_EATS", body)
    assert exc.value.code == "DELIVERY_PRODUCTION_CREDENTIALS_MISSING"

    with patch.multiple(get_settings(), UBER_EATS_CLIENT_ID="id", UBER_EATS_CLIENT_SECRET="secret"):
        result, db = await _admin_put("UBER_EATS", body)
    assert result.status == "CONNECTED" and result.activated_at is not None
    assert db.added[-1].diff_json["uber_approval_reference"] == "UBER-APPROVAL-123"


@pytest.mark.parametrize("status", ["SANDBOX", "AWAITING_APPROVAL", "CONNECTED", "ATTENTION_REQUIRED"])
async def test_mr_d_can_never_be_set_live_pending_partner_approval(status):
    body = {"status": status, "environment": "sandbox" if status == "SANDBOX" else "production",
            "external_store_id": "s", "uber_approval_reference": "x" * 5}
    with pytest.raises(AppError) as exc:
        await _admin_put("MR_D", body)
    assert exc.value.code == "MRD_PENDING_PARTNER_APPROVAL"


def test_admin_connection_routes_are_super_admin_only_and_never_return_a_token():
    from admin_api.app.api.v1 import routes_delivery_connections as admin_routes
    assert "provisioning_token_encrypted" not in admin_routes.AdminDeliveryConnectionResponse.model_fields
    for route in admin_routes.router.routes:
        deps = [d.call for d in route.dependant.dependencies]
        assert any(getattr(d, "__name__", "") in ("_check", "dependency") or "super_admin" in repr(d) for d in deps)


# ── CSV report import ────────────────────────────────────────────────────────

def test_order_total_column_is_taken_once_not_summed_per_line():
    from backend.app.external_orders.adapters import adapt_rows

    rows = [
        {"Order ID": "A1", "Date": "2026-10-26 12:00", "Item": "Margherita", "Qty": "1",
         "Item Price": "100.00", "Order Total": "300.00"},
        {"Order ID": "A1", "Date": "2026-10-26 12:00", "Item": "Garlic Bread", "Qty": "2",
         "Item Price": "200.00", "Order Total": "300.00"},
    ]
    result = adapt_rows(rows, platform="UBER_EATS")
    assert result.resolved_mapping["order_total"] == "Order Total"
    assert result.resolved_mapping["price"] == "Item Price"
    assert result.orders[0].gross_cents == 30000, "R300 once — not R900"


def test_line_prices_still_sum_when_there_is_no_order_total_column():
    from backend.app.external_orders.adapters import adapt_rows

    rows = [
        {"Order ID": "A1", "Date": "2026-10-26 12:00", "Item": "Margherita", "Qty": "1", "Price": "100.00"},
        {"Order ID": "A1", "Date": "2026-10-26 12:00", "Item": "Garlic Bread", "Qty": "2", "Price": "200.00"},
    ]
    assert adapt_rows(rows, platform="UBER_EATS").orders[0].gross_cents == 30000


def test_live_api_match_by_id_or_by_display_code_inside_the_window_only():
    from backend.app.external_orders.service import _matches_live_order

    placed = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    live = [DeliveryOrder(external_order_id="uuid-1", display_id="BC953", placed_at=placed)]
    assert _matches_live_order("uuid-1", None, live) is True
    assert _matches_live_order("BC953", placed + timedelta(hours=3), live) is True
    assert _matches_live_order("BC953", placed + timedelta(days=3), live) is False, "reused short code"
    assert _matches_live_order("BC953", None, live) is False, "no time to compare is not proof"
    assert _matches_live_order("ZZ999", placed, live) is False


# ── Per-environment credentials, hosts and tokens ───────────────────────────

class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


async def test_each_environment_mints_with_its_own_app_on_its_own_host_and_cache_key():
    from backend.app.delivery.uber.auth import get_operational_token, invalidate_operational_token

    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.host, dict(httpx.QueryParams(request.content.decode()))))
        return httpx.Response(200, json={"access_token": f"tok-{request.url.host}", "expires_in": 2592000})

    redis = _FakeRedis()
    with patch.multiple(
        get_settings(), UBER_EATS_CLIENT_ID="prod-id", UBER_EATS_CLIENT_SECRET="prod-secret",
        UBER_EATS_SANDBOX_CLIENT_ID="sb-id", UBER_EATS_SANDBOX_CLIENT_SECRET="sb-secret",
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            prod = await get_operational_token(redis, client, "production")
            sandbox = await get_operational_token(redis, client, "sandbox")
            again = await get_operational_token(redis, client, "sandbox")
            await invalidate_operational_token(redis, "sandbox")
            await get_operational_token(redis, client, "sandbox")

    assert prod == "tok-auth.uber.com" and sandbox == again == "tok-sandbox-login.uber.com"
    assert seen[0][1]["client_id"] == "prod-id" and seen[1][1]["client_id"] == "sb-id"
    assert len(seen) == 3, "cached per environment; re-minted only after invalidation"
    assert "uber_eats:oauth:operational_token" in redis.store, "production keeps its pre-existing cache key"
    assert "uber_eats:sandbox:oauth:operational_token" in redis.store


async def test_unknown_environment_is_refused_not_defaulted():
    from backend.app.delivery.uber.environments import get_environment_config
    with pytest.raises(DeliveryProviderError):
        get_environment_config("staging")


async def test_a_401_drops_the_cached_token_and_retries_exactly_once():
    from backend.app.delivery.uber import adapter as uber_adapter

    tokens = iter(["stale", "fresh"])
    calls = []

    async def request(client, token, base):
        calls.append((token, base))
        if token == "stale":
            raise DeliveryProviderError("UBER_EATS", "unauthorized", 401)
        return "ok"

    context = DeliveryOrderContext(
        business_id=uuid.uuid4(), connection_id=uuid.uuid4(), external_store_id="s", environment="sandbox",
    )
    with patch.object(uber_adapter, "get_redis", AsyncMock(return_value=_FakeRedis())), \
         patch.object(uber_adapter, "get_operational_token", AsyncMock(side_effect=lambda *a, **k: next(tokens))), \
         patch.object(uber_adapter, "invalidate_operational_token", AsyncMock()) as invalidate:
        assert await uber_adapter._call(context, request) == "ok"
    assert calls == [("stale", "https://test-api.uber.com"), ("fresh", "https://test-api.uber.com")]
    invalidate.assert_awaited_once()


async def test_client_builds_documented_urls_and_bodies():
    from backend.app.delivery.uber import client

    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, str(request.url), request.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await client.cancel_order(http, "t", "ord-1", body={"reason": "OTHER", "details": "x"},
                                  api_base_url="https://test-api.uber.com")
        await client.mark_order_ready(http, "t", "ord-1", api_base_url="https://api.uber.com")
        await client.get_menu(http, "t", "store-1", api_base_url="https://api.uber.com")
        with pytest.raises(DeliveryProviderError):
            await client.mark_order_ready(http, "t", "../evil", api_base_url="https://api.uber.com")

    assert seen[0][:2] == ("POST", "https://test-api.uber.com/v1/eats/orders/ord-1/cancel")
    assert seen[1][:2] == ("POST", "https://api.uber.com/v1/delivery/order/ord-1/ready")
    assert seen[2][:2] == ("GET", "https://api.uber.com/v2/eats/stores/store-1/menus")
    assert len(seen) == 3, "an unsafe id never reaches the network"


async def test_restaurant_cancel_maps_unknown_reasons_to_other_with_required_details():
    from backend.app.delivery.uber import adapter as uber_adapter

    captured = {}

    async def fake_call(context, request):
        class C:
            async def post(self, url, headers, json, timeout):
                captured["url"], captured["json"] = url, json
                return httpx.Response(200, json={})
        return await request(C(), "t", "https://api.uber.com")

    context = DeliveryOrderContext(business_id=uuid.uuid4(), connection_id=uuid.uuid4(), external_store_id="s")
    with patch.object(uber_adapter, "_call", fake_call):
        await uber_adapter.UberEatsAdapter().cancel_order(
            external_order_id="ord-1", context=context, reason_code="MADE_UP", explanation=None,
        )
    assert captured["json"] == {"reason": "OTHER", "details": "Cancelled by the restaurant"}


# ── Intake: environment + scheduled deadline ────────────────────────────────

async def test_intake_records_the_signature_proven_environment_and_a_tighter_scheduled_deadline():
    import hashlib
    import hmac
    import json

    from backend.app.delivery import intake
    from shared.models.delivery import DeliveryConnection

    body = json.dumps({
        "event_type": "orders.scheduled.notification", "event_id": "evt-1",
        "meta": {"user_id": "store-1", "resource_id": "ord-1"},
    }).encode()
    headers = {
        "x-uber-signature": hmac.new(b"sb-secret", body, hashlib.sha256).hexdigest(),
        "x-environment": "production",  # a lying header must not win
    }
    connection = DeliveryConnection(id=uuid.uuid4(), business_id=uuid.uuid4(), provider="UBER_EATS",
                                    external_store_id="store-1", status="SANDBOX", environment="sandbox")
    inserted = {}

    async def fake_event(db, **kwargs):
        inserted["event"] = kwargs
        return types.SimpleNamespace(id=uuid.uuid4(), business_id=None, error_category=None), True

    async def fake_order(db, **kwargs):
        inserted["order"] = kwargs

    db = MagicMock(commit=AsyncMock(), flush=AsyncMock(), rollback=AsyncMock())
    pool = MagicMock(enqueue_job=AsyncMock())
    with patch.multiple(get_settings(), UBER_EATS_WEBHOOK_ENABLED=True, UBER_EATS_CLIENT_SECRET="",
                        UBER_EATS_SANDBOX_CLIENT_SECRET="sb-secret"), \
         patch.object(intake, "_insert_event_idempotent", fake_event), \
         patch.object(intake, "resolve_delivery_connection", AsyncMock(return_value=connection)), \
         patch.object(intake, "_insert_delivery_order_idempotent", fake_order):
        result = await intake.ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body, headers=headers, arq_pool=pool,
        )
    assert result.accepted
    assert inserted["event"]["environment"] == "sandbox"
    assert inserted["order"]["event_type"] == "orders.scheduled.notification"


def test_scheduled_deadline_stays_inside_ubers_ten_minute_window():
    from backend.app.delivery import intake

    settings = get_settings()
    seconds = min(
        settings.DELIVERY_ACCEPT_DEADLINE_SECONDS,
        intake._SCHEDULED_ACCEPT_WINDOW_SECONDS - settings.DELIVERY_DEADLINE_GRACE_SECONDS - 60,
    )
    worst_case = seconds + settings.DELIVERY_DEADLINE_GRACE_SECONDS + 30  # + one watchdog tick
    assert worst_case < 600


def test_normal_deadline_plus_grace_plus_a_tick_beats_ubers_auto_cancel():
    settings = get_settings()
    assert settings.DELIVERY_ACCEPT_DEADLINE_SECONDS + settings.DELIVERY_DEADLINE_GRACE_SECONDS + 30 < 690


# ── Retention sweep ──────────────────────────────────────────────────────────

async def test_retention_sweep_redacts_then_deletes_in_one_bounded_transaction():
    from backend.app.delivery.retention import delivery_webhook_retention_sweep

    statements = []
    session = MagicMock(commit=AsyncMock(), rollback=AsyncMock())

    async def execute(stmt):
        statements.append(stmt)
        return types.SimpleNamespace(rowcount=1)

    session.execute = execute
    cm = MagicMock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock(return_value=False))
    await delivery_webhook_retention_sweep({"session_factory": MagicMock(return_value=cm)})
    compiled = [str(s).upper() for s in statements]
    assert compiled[0].startswith("UPDATE DELIVERY_WEBHOOK_EVENTS") and "LIMIT" in compiled[0]
    assert compiled[1].startswith("DELETE FROM DELIVERY_WEBHOOK_EVENTS") and "LIMIT" in compiled[1]
    session.commit.assert_awaited_once()


def test_retention_sweep_is_scheduled_daily():
    from backend.app.worker import WorkerSettings
    names = [getattr(c.coroutine, "__name__", "") for c in WorkerSettings.cron_jobs]
    assert "delivery_webhook_retention_sweep" in names
    assert "sync_delivery_order_status" in [f.__name__ for f in WorkerSettings.functions]


# ── Production boot guard: sandbox-only deployments ─────────────────────────

def _boot(**overrides):
    from backend.app import main
    settings = main.settings
    secret = "x" * 48
    values = dict(
        ENVIRONMENT="production", JWT_SECRET_KEY=secret, JWT_ADMIN_SECRET_KEY="y" * 48,
        DATABASE_URL="postgresql+asyncpg://u:p@db.example.com:5432/n", REDIS_URL="redis://cache.example.com:6379/0",
        CREDENTIALS_ENCRYPTION_KEY="k", META_APP_SECRET="real-meta-secret", META_VERIFY_TOKEN="real-verify",
        WHATSAPP_DEFAULT_ACCESS_TOKEN="real-wa-token", SQL_ECHO_ENABLED=False, BILLING_ENABLED=False,
        UBER_EATS_WEBHOOK_ENABLED=True, UBER_EATS_CLIENT_ID="", UBER_EATS_CLIENT_SECRET="",
        UBER_EATS_SANDBOX_CLIENT_ID="", UBER_EATS_SANDBOX_CLIENT_SECRET="",
        UBER_EATS_SANDBOX_ONLY_DEPLOYMENT=False,
    )
    values.update(overrides)
    with patch.multiple(settings, **values):
        main._validate_production_secrets()


SANDBOX_PAIR = dict(
    UBER_EATS_SANDBOX_CLIENT_ID="sb-id-" + "a" * 40, UBER_EATS_SANDBOX_CLIENT_SECRET="sb-secret-" + "b" * 40,
)


def test_sandbox_only_deployment_boots_only_when_explicitly_declared():
    _boot(UBER_EATS_SANDBOX_ONLY_DEPLOYMENT=True, **SANDBOX_PAIR)


def test_a_sandbox_pair_alone_does_not_excuse_a_missing_production_secret():
    """
    Security review 2026-10-26: without the explicit flag, shipping a
    sandbox-only config to production would 200-ack every genuine production
    order as BAD_SIGNATURE (BUG-0089's silent-drop pattern).
    """
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_SECRET"):
        _boot(**SANDBOX_PAIR)


def test_no_uber_credentials_at_all_still_refuses_to_boot():
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_SECRET"):
        _boot()


def test_half_configured_sandbox_pair_refuses_to_boot():
    with pytest.raises(RuntimeError, match="UBER_EATS_SANDBOX_CLIENT_SECRET"):
        _boot(UBER_EATS_SANDBOX_CLIENT_ID="sb-id-" + "a" * 40)


def test_half_configured_production_pair_refuses_even_with_a_sandbox_pair():
    with pytest.raises(RuntimeError, match="UBER_EATS_CLIENT_SECRET"):
        _boot(
            UBER_EATS_CLIENT_ID="prod-id-" + "c" * 40,
            UBER_EATS_SANDBOX_CLIENT_ID="sb-id-" + "a" * 40, UBER_EATS_SANDBOX_CLIENT_SECRET="sb-secret-" + "b" * 40,
        )


# ── Security-review follow-ups (2026-10-26) ──────────────────────────────────

async def _ingest_signed(body: bytes, secret: bytes, connection, inserted_events: list):
    import hashlib
    import hmac

    from backend.app.delivery import intake

    async def fake_event(db, **kwargs):
        inserted_events.append(kwargs)
        return types.SimpleNamespace(id=uuid.uuid4(), business_id=None, error_category=None), True

    db = MagicMock(commit=AsyncMock(), flush=AsyncMock(), rollback=AsyncMock())
    pool = MagicMock(enqueue_job=AsyncMock())
    headers = {"x-uber-signature": hmac.new(secret, body, hashlib.sha256).hexdigest()}
    with patch.multiple(get_settings(), UBER_EATS_WEBHOOK_ENABLED=True,
                        UBER_EATS_CLIENT_SECRET="prod-secret", UBER_EATS_SANDBOX_CLIENT_SECRET="sb-secret"), \
         patch.object(intake, "_insert_event_idempotent", fake_event), \
         patch.object(intake, "resolve_delivery_connection", AsyncMock(return_value=connection)), \
         patch.object(intake, "_insert_delivery_order_idempotent", AsyncMock()) as insert_order:
        result = await intake.ingest_delivery_webhook(
            db, provider="UBER_EATS", raw_body=body, headers=headers, arq_pool=pool,
        )
    return result, pool, insert_order


@pytest.mark.parametrize("event_type", ["orders.cancel", "orders.notification", "orders.customer_order_edit"])
async def test_an_event_signed_for_the_other_environment_never_touches_the_connection(event_type):
    """
    A sandbox-signed cancel naming a PRODUCTION store/order used to reach the
    cancellation job, which has no environment gate — cancelling the
    production order and reversing its stock. Now refused at intake for
    every event type, before any row or job exists.
    """
    import json
    from shared.models.delivery import DeliveryConnection

    production = DeliveryConnection(id=uuid.uuid4(), business_id=uuid.uuid4(), provider="UBER_EATS",
                                    external_store_id="store-prod", status="CONNECTED", environment="production")
    body = json.dumps({"event_type": event_type, "event_id": "evt-9",
                       "meta": {"user_id": "store-prod", "resource_id": "real-order"}}).encode()
    events: list = []
    result, pool, insert_order = await _ingest_signed(body, b"sb-secret", production, events)
    assert result.error_category == "ENV_MISMATCH" and result.http_status == 200
    pool.enqueue_job.assert_not_awaited()
    insert_order.assert_not_awaited()


async def test_sandbox_event_ids_are_namespaced_so_they_cannot_shadow_production_ones():
    import json
    from shared.models.delivery import DeliveryConnection

    sandbox = DeliveryConnection(id=uuid.uuid4(), business_id=uuid.uuid4(), provider="UBER_EATS",
                                 external_store_id="store-sb", status="SANDBOX", environment="sandbox")
    body = json.dumps({"event_type": "orders.notification", "event_id": "evt-1",
                       "meta": {"user_id": "store-sb", "resource_id": "o-1"}}).encode()
    events: list = []
    await _ingest_signed(body, b"sb-secret", sandbox, events)
    assert events[0]["event_id"] == "sandbox:evt-1"

    production = DeliveryConnection(id=uuid.uuid4(), business_id=uuid.uuid4(), provider="UBER_EATS",
                                    external_store_id="store-sb", status="CONNECTED", environment="production")
    events.clear()
    await _ingest_signed(body, b"prod-secret", production, events)
    assert events[0]["event_id"] == "evt-1", "production keeps Uber's raw id, as before"


async def test_a_second_401_inside_the_cooldown_does_not_re_mint_the_shared_token():
    from backend.app.delivery.uber import adapter as uber_adapter

    async def always_401(client, token, base):
        raise DeliveryProviderError("UBER_EATS", "unauthorized", 401)

    redis = _FakeRedis()
    context = DeliveryOrderContext(business_id=uuid.uuid4(), connection_id=uuid.uuid4(), external_store_id="s")
    with patch.object(uber_adapter, "get_redis", AsyncMock(return_value=redis)), \
         patch.object(uber_adapter, "get_operational_token", AsyncMock(return_value="tok")), \
         patch.object(uber_adapter, "invalidate_operational_token", AsyncMock()) as invalidate:
        for _ in range(3):
            with pytest.raises(DeliveryProviderError):
                await uber_adapter._call(context, always_401)
    assert invalidate.await_count == 1, "one forced re-mint per cooldown window, however many 401s"


@pytest.mark.parametrize("bad_id", ["²", "Ａbc", "a/b", "id?x", "", "x" * 129])
def test_ids_interpolated_into_uber_urls_are_ascii_only(bad_id):
    from backend.app.delivery.uber import client
    with pytest.raises(DeliveryProviderError):
        client._validated_order_id(bad_id)


def test_admin_store_id_must_be_ascii_id_shaped():
    from admin_api.app.api.v1 import routes_delivery_connections as admin_routes
    with pytest.raises(Exception):
        admin_routes.AdminDeliveryConnectionUpsert(status="SANDBOX", environment="sandbox", external_store_id="störe")


async def test_admin_refuses_connected_on_a_declared_sandbox_only_deployment():
    body = {"status": "CONNECTED", "environment": "production", "external_store_id": "s",
            "uber_approval_reference": "UBER-APPROVAL-123"}
    with patch.multiple(get_settings(), UBER_EATS_CLIENT_ID="id", UBER_EATS_CLIENT_SECRET="secret",
                        UBER_EATS_SANDBOX_ONLY_DEPLOYMENT=True):
        with pytest.raises(AppError) as exc:
            await _admin_put("UBER_EATS", body)
    assert exc.value.code == "DELIVERY_PRODUCTION_CREDENTIALS_MISSING"


@pytest.mark.parametrize("role,expect_money", [("STAFF", False), ("MANAGER", True), ("OWNER", True)])
def test_staff_action_responses_carry_no_payout_figures(role, expect_money):
    user = make_user(role=role)
    row = make_order(user.business_id, status="ACCEPTED", gross_cents=7000, fee_cents=500,
                     fetched_order_snapshot_json={"lines": [
                         {"title": "Muffin", "quantity": 2, "unit_price_cents": 3500, "total_price_cents": 7000, "modifiers": []},
                     ]})
    response = routes._order_response_for(user, row)
    assert (response.gross_cents is not None) is expect_money
    assert (response.fee_cents is not None) is expect_money
    assert (response.lines[0]["unit_price_cents"] is not None) is expect_money
    assert response.status == "ACCEPTED" and response.lines[0]["title"] == "Muffin"
