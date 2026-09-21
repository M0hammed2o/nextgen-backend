"""
Wave 0A / Milestone C — MetaGraphClient unit tests. All httpx calls are
mocked (matching the existing mocking style for backend/app/payments/ikhoka.py
in tests/test_payment_flow.py) — no network access anywhere in this file.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.whatsapp.graph_client import GraphAPIError, MetaGraphClient


def _mock_httpx_client(status_code: int, json_body: dict, raise_exc: Exception | None = None):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = json_body
    mock_response.text = str(json_body)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if raise_exc is not None:
        mock_client.request = AsyncMock(side_effect=raise_exc)
    else:
        mock_client.request = AsyncMock(return_value=mock_response)
    return mock_client


async def test_exchange_code_for_token_success():
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(200, {"access_token": "abc123", "token_type": "bearer"})
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        data = await client.exchange_code_for_token("some-code")
    assert data["access_token"] == "abc123"


async def test_graph_error_parses_code_and_subcode():
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(
        401, {"error": {"message": "Error validating access token", "code": 190, "error_subcode": 463}}
    )
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GraphAPIError) as exc_info:
            await client.get_waba("waba123", "bad-token")
    exc = exc_info.value
    assert exc.status_code == 401
    assert exc.error_code == 190
    assert exc.error_subcode == 463
    assert exc.is_invalid_token is True


async def test_graph_error_non_190_is_not_invalid_token():
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(500, {"error": {"message": "Internal error", "code": 1}})
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GraphAPIError) as exc_info:
            await client.subscribe_app("waba123", "token")
    assert exc_info.value.is_invalid_token is False


def test_is_already_registered_heuristic():
    exc = GraphAPIError("This phone number is already registered", raw={"message": "This phone number is already registered"})
    assert exc.is_already_registered is True

    exc2 = GraphAPIError("Some unrelated failure", raw={"message": "Some unrelated failure"})
    assert exc2.is_already_registered is False


async def test_timeout_raises_graph_api_error():
    import httpx as httpx_module
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(200, {}, raise_exc=httpx_module.TimeoutException("timed out"))
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GraphAPIError):
            await client.get_phone_number("phone123", "token")


async def test_send_message_sets_messaging_product_default():
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(200, {"messages": [{"id": "wamid.abc"}]})
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        data = await client.send_message("phone123", "token", {"type": "text", "text": {"body": "hi"}})
    assert data["messages"][0]["id"] == "wamid.abc"
    call_kwargs = mock_client.request.call_args.kwargs
    assert call_kwargs["json"]["messaging_product"] == "whatsapp"


async def test_register_phone_tolerable_already_registered_error():
    client = MetaGraphClient()
    mock_client = _mock_httpx_client(
        400, {"error": {"message": "This phone number is already registered", "code": 100}}
    )
    with patch("backend.app.whatsapp.graph_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GraphAPIError) as exc_info:
            await client.register_phone("phone123", "token", "123456")
    assert exc_info.value.is_already_registered is True
