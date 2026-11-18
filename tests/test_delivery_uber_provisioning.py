"""
Uber Eats store activation (Integration Configuration API) — `provisioning.py`
and the two client calls it uses. Network is mocked; the request shapes are
the ones Uber documents (read 2026-10-29).
"""
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from backend.app.core.config import get_settings
from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout
from backend.app.delivery.uber import provisioning

SB_SECRET = "sandbox-secret-value-not-in-any-message"


@pytest.fixture(autouse=True)
def uber_settings():
    with patch.multiple(
        get_settings(), UBER_EATS_CLIENT_ID="prod-id", UBER_EATS_CLIENT_SECRET="prod-secret",
        UBER_EATS_SANDBOX_CLIENT_ID="sb-id", UBER_EATS_SANDBOX_CLIENT_SECRET=SB_SECRET,
    ):
        yield


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_authorize_url_uses_the_environments_auth_host_and_the_provisioning_scope():
    url = urlparse(provisioning.build_authorize_url(
        "sandbox", redirect_uri="http://localhost:8766/uber/oauth/callback", state="st-1",
    ))
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert (url.scheme, url.netloc, url.path) == ("https", "sandbox-login.uber.com", "/oauth/v2/authorize")
    assert query == {
        "client_id": "sb-id", "response_type": "code", "scope": "eats.pos_provisioning",
        "redirect_uri": "http://localhost:8766/uber/oauth/callback", "state": "st-1",
    }
    prod = urlparse(provisioning.build_authorize_url("production", redirect_uri="https://x/cb", state="s"))
    assert prod.netloc == "auth.uber.com" and "prod-id" in prod.query


async def test_authorize_url_refuses_without_a_client_id():
    with patch.object(get_settings(), "UBER_EATS_SANDBOX_CLIENT_ID", ""):
        with pytest.raises(DeliveryProviderError):
            provisioning.build_authorize_url("sandbox", redirect_uri="http://localhost/cb", state="s")


async def test_code_exchange_posts_the_documented_form_to_the_sandbox_token_endpoint():
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), {k: v[0] for k, v in parse_qs(request.content.decode()).items()}))
        return httpx.Response(200, json={
            "access_token": "merchant-token", "token_type": "Bearer", "expires_in": 2592000,
            "scope": "eats.pos_provisioning",
        })

    async with _client(handler) as http:
        token = await provisioning.exchange_authorization_code(
            http, "sandbox", code="one-time-code", redirect_uri="http://localhost:8766/uber/oauth/callback",
        )
    assert token == "merchant-token"
    assert seen == [("https://sandbox-login.uber.com/oauth/v2/token", {
        "client_id": "sb-id", "client_secret": SB_SECRET, "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:8766/uber/oauth/callback", "code": "one-time-code",
    })]


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "invalid_grant"}),
    httpx.Response(200, json={"token_type": "Bearer"}),
    httpx.Response(200, json={"access_token": "t", "scope": "eats.store"}),
])
async def test_code_exchange_refuses_rejections_missing_tokens_and_the_wrong_scope(response):
    async with _client(lambda request: response) as http:
        with pytest.raises(DeliveryProviderError) as raised:
            await provisioning.exchange_authorization_code(http, "sandbox", code="c0de-value", redirect_uri="r")
    assert SB_SECRET not in str(raised.value) and "c0de-value" not in str(raised.value)


async def test_code_exchange_timeout_is_a_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as http:
        with pytest.raises(DeliveryProviderTimeout):
            await provisioning.exchange_authorization_code(http, "sandbox", code="c", redirect_uri="r")


async def test_store_listing_follows_next_key_on_the_sandbox_api_host():
    seen = []
    pages = [
        {"stores": [{"store_id": "store-a", "name": "A"}], "next_key": "k&2=x"},
        {"stores": [{"store_id": "store-b"}, {"name": "no id"}], "next_key": ""},
    ]

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    async with _client(handler) as http:
        ids = await provisioning.list_merchant_store_ids(http, "merchant-token", environment="sandbox")
    assert ids == ["store-a", "store-b"]
    assert [r.url.host for r in seen] == ["test-api.uber.com"] * 2
    assert seen[0].url.path == "/v1/eats/stores" and "start_key" not in seen[0].url.params
    assert seen[1].url.params["start_key"] == "k&2=x", "Uber's key is passed back URL-encoded, verbatim"
    assert seen[0].headers["authorization"] == "Bearer merchant-token"


async def test_activation_sends_only_documented_fields_to_pos_data():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(204)

    async with _client(handler) as http:
        await provisioning.activate_store(
            http, "merchant-token", environment="sandbox",
            store_id="0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21", integrator_store_id="biz-1",
        )
        with pytest.raises(DeliveryProviderError):
            await provisioning.activate_store(
                http, "merchant-token", environment="sandbox", store_id="../evil", integrator_store_id="biz-1",
            )

    assert len(seen) == 1, "an unsafe store id never reaches the network"
    request = seen[0]
    assert request.method == "POST"
    assert (request.url.host, request.url.path) == (
        "test-api.uber.com", "/v1/eats/stores/0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21/pos_data")
    assert json.loads(request.content) == {"is_order_manager": True, "integrator_store_id": "biz-1"}
    # Uber's reference lists the fields as query params; its example sends a body. Both carry them.
    assert dict(request.url.params) == {"is_order_manager": "true", "integrator_store_id": "biz-1"}


async def test_integration_details_is_a_read_only_get_of_pos_data():
    from backend.app.delivery.uber import client
    seen = []
    record = {"store_id": "s", "integration_enabled": True, "order_manager_client_id": "abc"}

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json=record)

    async with _client(handler) as http:
        details = await client.get_integration_details(
            http, "app-token", "0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21", api_base_url="https://test-api.uber.com")
        with pytest.raises(DeliveryProviderError):
            await client.get_integration_details(http, "app-token", "../evil", api_base_url="https://test-api.uber.com")
    assert details == record
    assert len(seen) == 1, "an unsafe store id never reaches the network"
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://test-api.uber.com/v1/eats/stores/0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21/pos_data"
    assert seen[0].headers["authorization"] == "Bearer app-token"


async def test_integration_details_before_activation_raises_with_ubers_403():
    from backend.app.delivery.uber import client
    async with _client(lambda r: httpx.Response(403, json={"code": "user_not_allowed"})) as http:
        with pytest.raises(DeliveryProviderError) as raised:
            await client.get_integration_details(http, "t", "store-1", api_base_url="https://test-api.uber.com")
    assert raised.value.status_code == 403


async def test_activation_rejected_by_uber_raises_with_the_status():
    async with _client(lambda request: httpx.Response(403, json={"code": "forbidden"})) as http:
        with pytest.raises(DeliveryProviderError) as raised:
            await provisioning.activate_store(
                http, "t", environment="sandbox", store_id="store-1", integrator_store_id="biz-1",
            )
    assert raised.value.status_code == 403


# ── The activation tool's one-shot redirect listener ────────────────────────

import importlib.util  # noqa: E402
import pathlib  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402


def _load_tool():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "uber_activate_store.py"
    spec = importlib.util.spec_from_file_location("uber_activate_store", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_listener_ignores_wrong_paths_and_wrong_state_and_returns_the_matching_redirect():
    tool = _load_tool()
    port = _free_port()
    listener = tool.RedirectListener(port, "/uber/oauth/callback", "the-state")
    result: dict = {}
    waiter = threading.Thread(target=lambda: result.update(listener.wait(timeout_seconds=20)))
    waiter.start()
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/elsewhere?code=x&state=the-state").status_code == 404
    assert httpx.get(f"{base}/uber/oauth/callback?code=x&state=forged").status_code == 400
    assert httpx.get(f"{base}/uber/oauth/callback?code=x&state=%C3%A9").status_code == 400  # non-ASCII state
    ok = httpx.get(f"{base}/uber/oauth/callback?code=the-code&state=the-state")
    waiter.join(timeout=20)
    assert ok.status_code == 200 and "the-code" not in ok.text
    assert result == {"code": "the-code", "state": "the-state"}


def test_listener_gives_up_on_a_real_clock_deadline_even_with_an_idle_connection_open():
    tool = _load_tool()
    port = _free_port()
    listener = tool.RedirectListener(port, "/cb", "s")
    idle = socket.create_connection(("127.0.0.1", port))  # connects, never sends a request
    try:
        result = listener.wait(timeout_seconds=1.5)
    finally:
        idle.close()
    assert result == {}


async def test_activation_tool_reads_ubers_integration_record_back(capsys):
    """After POST pos_data the tool reads GET pos_data with the app token and reports it without printing ids."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    tool = _load_tool()
    business_id = "c7c244dd-d4f0-49e5-85fc-52b8dd16575c"
    connection = SimpleNamespace(external_store_id="0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21", business_id=business_id)
    env = SimpleNamespace(api_base_url="https://test-api.uber.com", client_id="sandbox-client-id-value")
    record = {"integration_enabled": True, "is_order_manager_pending": False,
              "order_manager_client_id": "sandbox-client-id-value", "integrator_store_id": business_id}
    with patch.object(tool.provisioning, "exchange_authorization_code", AsyncMock(return_value="merchant-token")), \
            patch.object(tool.provisioning, "list_merchant_store_ids", AsyncMock(return_value=[connection.external_store_id])), \
            patch.object(tool.provisioning, "activate_store", AsyncMock()) as activate, \
            patch.object(tool, "get_redis", AsyncMock()), \
            patch.object(tool.auth, "get_operational_token", AsyncMock(return_value="app-token")), \
            patch.object(tool, "get_environment_config", return_value=env), \
            patch.object(tool.client, "get_integration_details", AsyncMock(return_value=record)) as details, \
            patch.object(tool.client, "get_store_status", AsyncMock(return_value={"status": "ONLINE"})):
        await tool._activate(connection, "code", "https://example.test/cb")
    out = capsys.readouterr().out
    activate.assert_awaited_once()
    assert details.await_args.args[1:3] == ("app-token", connection.external_store_id)
    assert "integration_enabled=True" in out
    assert "order manager is this app=True" in out
    assert "integrator_store_id matches=True" in out
    assert "sandbox-client-id-value" not in out and "merchant-token" not in out and "app-token" not in out


async def test_activation_tool_reports_a_403_integration_record_without_failing(capsys):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    tool = _load_tool()
    connection = SimpleNamespace(external_store_id="0b6f2a3e-1d2c-4e5f-8a9b-7c6d5e4f3a21", business_id="biz")
    env = SimpleNamespace(api_base_url="https://test-api.uber.com", client_id="cid")
    with patch.object(tool.provisioning, "exchange_authorization_code", AsyncMock(return_value="m")), \
            patch.object(tool.provisioning, "list_merchant_store_ids", AsyncMock(return_value=[connection.external_store_id])), \
            patch.object(tool.provisioning, "activate_store", AsyncMock()), \
            patch.object(tool, "get_redis", AsyncMock()), \
            patch.object(tool.auth, "get_operational_token", AsyncMock(return_value="a")), \
            patch.object(tool, "get_environment_config", return_value=env), \
            patch.object(tool.client, "get_integration_details",
                         AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "x", status_code=403))), \
            patch.object(tool.client, "get_store_status",
                         AsyncMock(side_effect=DeliveryProviderError("UBER_EATS", "x", status_code=401))):
        await tool._activate(connection, "code", "https://example.test/cb")
    out = capsys.readouterr().out
    assert "not readable yet: HTTP 403" in out and "still fails: HTTP 401" in out
