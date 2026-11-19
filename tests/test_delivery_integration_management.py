"""
Uber go-live items 5 and 6: integration update/removal and store prep time.

- PATCH /v1/eats/stores/{id}/pos_data (Update Integration Details, scope eats.store)
- POST /v1/delivery/store/{id}/update-store-prep-time ({"default_prep_time": seconds})
- store.deprovisioned webhook: the provider removed NextGen from the store
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.api.v1 import routes_delivery as routes
from backend.app.core.errors import AppError
from backend.app.delivery.base import DeliveryOrderContext
from backend.app.delivery.errors import DeliveryProviderError
from backend.app.delivery.intake import ingest_delivery_webhook
from backend.app.delivery.mrd.adapter import MrDAdapter
from backend.app.delivery.uber import client as uber_client
from backend.app.delivery.uber.adapter import UberEatsAdapter
from shared.models.delivery import DeliveryConnection, DeliveryWebhookEvent
from tests.test_delivery_intake import FakeArqPool, FakeDB as IntakeDB, FakeResult, _settings_with_webhook_enabled, _signed
from tests.test_routes_delivery import FakeDB, FakeScalarResult, make_business, make_connection, make_user


def _transport(captured: dict, status: int = 204) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content or b"{}")
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(status)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestClientCalls:
    async def test_update_integration_is_a_patch_to_pos_data(self):
        captured = {}
        async with _transport(captured) as c:
            await uber_client.update_integration_details(
                c, "tok", "store-1", body={"integration_enabled": False}, api_base_url="https://test-api.uber.com")
        assert captured["method"] == "PATCH"
        assert captured["url"] == "https://test-api.uber.com/v1/eats/stores/store-1/pos_data"
        assert captured["body"] == {"integration_enabled": False}
        assert captured["auth"] == "Bearer tok"

    async def test_prep_time_posts_seconds(self):
        captured = {}
        async with _transport(captured) as c:
            await uber_client.update_store_prep_time(
                c, "tok", "store-1", prep_time_seconds=1200, api_base_url="https://test-api.uber.com")
        assert captured["method"] == "POST"
        assert captured["url"] == "https://test-api.uber.com/v1/delivery/store/store-1/update-store-prep-time"
        assert captured["body"] == {"default_prep_time": 1200}

    async def test_provider_refusal_is_an_error_not_a_silent_success(self):
        async with _transport({}, status=403) as c:
            with pytest.raises(DeliveryProviderError):
                await uber_client.update_integration_details(
                    c, "tok", "store-1", body={"integration_enabled": False}, api_base_url="https://x")

    async def test_unsafe_store_id_never_reaches_a_url(self):
        async with _transport({}) as c:
            with pytest.raises(DeliveryProviderError):
                await uber_client.update_store_prep_time(c, "tok", "../admin", prep_time_seconds=60, api_base_url="https://x")


class TestCapabilities:
    def test_uber_advertises_what_it_actually_does(self):
        caps = UberEatsAdapter().capabilities
        assert caps.integration_management and caps.prep_time
        assert caps.menu_sync is False   # nothing uploads a menu; it used to claim this

    async def test_mr_d_refuses_honestly(self):
        from backend.app.delivery.errors import DeliveryCapabilityUnsupported
        ctx = DeliveryOrderContext(business_id=uuid.uuid4(), connection_id=uuid.uuid4(),
                                   external_store_id="s", environment="production")
        with pytest.raises(DeliveryCapabilityUnsupported):
            await MrDAdapter().update_prep_time(context=ctx, prep_time_seconds=600)

    async def test_adapter_sends_only_what_was_asked(self):
        ctx = DeliveryOrderContext(business_id=uuid.uuid4(), connection_id=uuid.uuid4(),
                                   external_store_id="store-1", environment="sandbox")
        seen = {}

        async def fake_call(context, fn):
            with patch.object(uber_client, "update_integration_details", new=AsyncMock()) as patch_call:
                await fn(MagicMock(), "tok", "https://test-api.uber.com")
                seen["body"] = patch_call.call_args.kwargs["body"]

        with patch("backend.app.delivery.uber.adapter._call", new=fake_call):
            await UberEatsAdapter().update_integration(context=ctx, integrator_store_id="ng-123")
        assert seen["body"] == {"integrator_store_id": "ng-123"}


def _db(user, connection, extra=2):
    return FakeDB([FakeScalarResult(make_business(user.business_id)), FakeScalarResult(connection)]
                  + [FakeScalarResult(None)] * extra)


class TestRoutes:
    async def test_switching_off_calls_uber_first_then_disables_and_audits(self):
        user = make_user(role="OWNER")
        connection = make_connection(user.business_id, status="CONNECTED")
        db = _db(user, connection)
        adapter = MagicMock(update_integration=AsyncMock())
        with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
            result = await routes.update_provider_integration(
                provider="UBER_EATS", body=routes.IntegrationUpdateRequest(integration_enabled=False), user=user, db=db)
        assert adapter.update_integration.call_args.kwargs["integration_enabled"] is False
        assert result.status == "DISABLED" and connection.disconnected_at is not None
        assert db.added[-1].action == "delivery.connection.provider_integration_changed"

    async def test_switching_back_on_never_writes_connected(self):
        user = make_user(role="OWNER")
        connection = make_connection(user.business_id, status="DISABLED")
        db = _db(user, connection)
        with patch("backend.app.delivery.registry.get_provider",
                   return_value=MagicMock(update_integration=AsyncMock())):
            result = await routes.update_provider_integration(
                provider="UBER_EATS", body=routes.IntegrationUpdateRequest(integration_enabled=True), user=user, db=db)
        assert result.status == "ATTENTION_REQUIRED"

    async def test_switching_back_on_a_live_store_is_refused(self):
        """Security review L1: same state rules as /enable."""
        user = make_user(role="OWNER")
        connection = make_connection(user.business_id, status="CONNECTED")
        adapter = MagicMock(update_integration=AsyncMock())
        with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
            with pytest.raises(AppError) as exc:
                await routes.update_provider_integration(
                    provider="UBER_EATS", body=routes.IntegrationUpdateRequest(integration_enabled=True),
                    user=user, db=_db(user, connection, extra=0))
        assert exc.value.code == "DELIVERY_CONNECTION_NOT_DISABLED"
        adapter.update_integration.assert_not_awaited()
        assert connection.status == "CONNECTED"

    async def test_switching_off_a_never_connected_store_is_refused(self):
        user = make_user(role="OWNER")
        connection = make_connection(user.business_id, status="NOT_CONNECTED")
        with pytest.raises(AppError) as exc:
            await routes.update_provider_integration(
                provider="UBER_EATS", body=routes.IntegrationUpdateRequest(integration_enabled=False),
                user=user, db=_db(user, connection, extra=0))
        assert exc.value.code == "DELIVERY_CONNECTION_ALREADY_INACTIVE"

    async def test_provider_failure_changes_nothing(self):
        user = make_user(role="OWNER")
        connection = make_connection(user.business_id, status="CONNECTED")
        db = _db(user, connection, extra=0)
        adapter = MagicMock(update_integration=AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "x", status_code=500)))
        with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
            with pytest.raises(AppError) as exc:
                await routes.update_provider_integration(
                    provider="UBER_EATS", body=routes.IntegrationUpdateRequest(integration_enabled=False),
                    user=user, db=db)
        assert exc.value.code == "DELIVERY_PROVIDER_UNAVAILABLE"
        assert connection.status == "CONNECTED" and db.commits == 0 and db.added == []

    async def test_prep_time_sends_seconds_and_audits(self):
        user = make_user(role="MANAGER")
        connection = make_connection(user.business_id, status="CONNECTED")
        db = _db(user, connection, extra=0)
        adapter = MagicMock(update_prep_time=AsyncMock())
        with patch("backend.app.delivery.registry.get_provider", return_value=adapter):
            result = await routes.update_prep_time(
                provider="UBER_EATS", body=routes.PrepTimeRequest(prep_time_minutes=25), user=user, db=db)
        assert adapter.update_prep_time.call_args.kwargs["prep_time_seconds"] == 1500
        assert result == {"provider": "UBER_EATS", "prep_time_minutes": 25}
        assert db.added[-1].action == "delivery.connection.prep_time_changed"

    def test_prep_time_is_bounded_by_ubers_three_hours(self):
        with pytest.raises(Exception):
            routes.PrepTimeRequest(prep_time_minutes=181)
        with pytest.raises(Exception):
            routes.PrepTimeRequest(prep_time_minutes=0)


class TestDeprovisionWebhook:
    async def _ingest(self, monkeypatch, status="CONNECTED"):
        _settings_with_webhook_enabled(monkeypatch)
        body = json.dumps({"event_type": "store.deprovisioned", "store_id": "store-1",
                           "webhook_meta": {"webhook_msg_uuid": str(uuid.uuid4())}}).encode()
        connection = DeliveryConnection(id=uuid.uuid4(), business_id=uuid.uuid4(), provider="UBER_EATS",
                                        external_store_id="store-1", status=status, environment="production")
        db = IntakeDB(execute_results=[FakeResult(scalar=uuid.uuid4()), FakeResult(scalar=connection)],
                      get_results=[DeliveryWebhookEvent(id=uuid.uuid4())])
        pool = FakeArqPool()
        with patch("backend.app.push_service.send_business_notification", new=AsyncMock()) as push:
            result = await ingest_delivery_webhook(
                db, provider="UBER_EATS", raw_body=body, headers={"X-Uber-Signature": _signed(body)}, arq_pool=pool)
        return result, connection, db, pool, push

    async def test_deprovision_flags_the_connection_and_alerts(self, monkeypatch):
        result, connection, db, pool, push = await self._ingest(monkeypatch)
        assert result.accepted and result.http_status == 200
        assert connection.status == "ATTENTION_REQUIRED" and connection.disconnected_at is not None
        assert "removed this integration" in connection.error_reason
        assert any(getattr(a, "action", None) == "delivery.connection.deprovisioned" for a in db.added)
        push.assert_awaited_once()
        assert pool.enqueued == []

    async def test_an_already_disabled_connection_is_left_alone(self, monkeypatch):
        _, connection, _, _, push = await self._ingest(monkeypatch, status="DISABLED")
        assert connection.status == "DISABLED"
        push.assert_not_awaited()
