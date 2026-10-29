"""
`routes_delivery_webhook.py` — raw-body-before-parsing ordering, the fixed
rate-limit scope (BUG-0077's exact class of mistake), the always-200-except-
enqueue-failure contract, and the "no arq_pool configured" 503 path.
"""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.v1 import routes_delivery_webhook
from backend.app.delivery.canonical import WebhookGateResult


class FakeRequestState:
    def __init__(self, arq_pool):
        self.arq_pool = arq_pool


class FakeApp:
    def __init__(self, arq_pool):
        self.state = FakeRequestState(arq_pool)


class FakeClient:
    host = "203.0.113.7"


class FakeRequest:
    def __init__(self, body: bytes, headers: dict, arq_pool):
        self._body = body
        self.headers = headers
        self.app = FakeApp(arq_pool)
        self.client = FakeClient()

    async def body(self) -> bytes:
        return self._body


class TestRateLimitScope:
    def test_route_uses_a_fixed_shared_limit_scope_not_the_path_default(self):
        """
        BUG-0077's exact mistake: `{provider}` is a path parameter, so a
        plain `limiter.limit(...)` would bucket per (IP, concrete path).
        `shared_limit(..., scope=...)` is the only API that accepts an
        explicit scope at all.
        """
        import inspect

        source = inspect.getsource(routes_delivery_webhook)
        assert "shared_limit" in source
        assert routes_delivery_webhook._RATE_LIMIT_SCOPE == "delivery_webhook"
        # Must NOT use the scope-less .limit() decorator for this route.
        assert "@limiter.limit(" not in source


class TestWebhookRouteHandler:
    """
    Uber's webhook reference: "Your service should return an HTTP 200
    response code with an empty response body". Every outcome is therefore
    a bare status code — the gate result lives in the log line and in
    delivery_webhook_events.error_category, never in the response (which
    also used to tell a forger whether their signature was rejected).
    """

    async def test_reads_raw_body_before_any_parsing(self, monkeypatch):
        """
        `request.body()` must be awaited and its exact bytes passed through
        to `ingest_delivery_webhook` — the signature is over the raw body.
        """
        raw = b'{"event_id": "e1"}'
        pool = AsyncMock()
        request = FakeRequest(raw, headers={}, arq_pool=pool)

        captured = {}

        async def fake_ingest(db, *, provider, raw_body, headers, arq_pool):
            captured["raw_body"] = raw_body
            captured["provider"] = provider
            return WebhookGateResult(accepted=True, error_category="OK")

        with patch.object(routes_delivery_webhook, "ingest_delivery_webhook", fake_ingest):
            result = await routes_delivery_webhook.delivery_webhook.__wrapped__(
                "UBER_EATS", request, db=MagicMock(),
            )
        assert captured["raw_body"] == raw
        assert captured["provider"] == "UBER_EATS"
        assert result.status_code == 200
        assert result.body == b""

    async def test_no_arq_pool_returns_503_never_a_false_200(self):
        request = FakeRequest(b"{}", headers={}, arq_pool=None)

        result = await routes_delivery_webhook.delivery_webhook.__wrapped__(
            "UBER_EATS", request, db=MagicMock(),
        )
        assert result.status_code == 503
        assert result.body == b""

    async def test_gate_result_status_code_is_propagated_onto_the_response(self):
        pool = AsyncMock()
        request = FakeRequest(b"{}", headers={}, arq_pool=pool)

        async def fake_ingest(db, *, provider, raw_body, headers, arq_pool):
            return WebhookGateResult(accepted=False, http_status=503, error_category="ENQUEUE_FAILED")

        with patch.object(routes_delivery_webhook, "ingest_delivery_webhook", fake_ingest):
            result = await routes_delivery_webhook.delivery_webhook.__wrapped__(
                "UBER_EATS", request, db=MagicMock(),
            )
        assert result.status_code == 503
        assert result.body == b""

    async def test_gate_rejection_still_returns_200_with_an_empty_body(self):
        pool = AsyncMock()
        request = FakeRequest(b"{}", headers={}, arq_pool=pool)

        async def fake_ingest(db, *, provider, raw_body, headers, arq_pool):
            return WebhookGateResult(accepted=False, http_status=200, error_category="BAD_SIGNATURE")

        with patch.object(routes_delivery_webhook, "ingest_delivery_webhook", fake_ingest):
            result = await routes_delivery_webhook.delivery_webhook.__wrapped__(
                "MR_D", request, db=MagicMock(),
            )
        assert result.status_code == 200
        assert result.body == b"", "the response must not reveal that the signature was rejected"


class TestRouteRegistration:
    def test_route_path_has_no_tenant_component(self):
        for route in routes_delivery_webhook.router.routes:
            assert "business_id" not in route.path
            assert route.path == "/webhook/delivery/{provider}"

    def test_route_is_mounted_publicly_with_no_rbac_dependency(self):
        for route in routes_delivery_webhook.router.routes:
            # No require_* dependency should be present on this route.
            dep_names = [
                getattr(d.call, "__name__", "") for d in getattr(route, "dependencies", [])
            ]
            assert not any(name.startswith("require_") for name in dep_names)
