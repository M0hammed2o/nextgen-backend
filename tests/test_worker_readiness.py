"""
Fix group 4 (wave0a/sec) — worker production definition, heartbeat, readiness.

Covers:
  - write_worker_heartbeat: writes a Redis key with a TTL.
  - GET /ready: 200 when DB/Redis/worker heartbeat are all healthy; 503 when
    DB or Redis is down; worker "stale" behaviour under both
    READY_REQUIRES_WORKER values; never leaks exception text.
  - routes_webhook.py: ARQ enqueue failure returns 5xx (not 200) so Meta
    retries; Meta hub.verify_token comparison uses hmac.compare_digest.
  - realtime/sse.py: rejects a suspended business / deactivated staff member.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.worker import WORKER_HEARTBEAT_TTL_SECONDS, write_worker_heartbeat


# ── Worker heartbeat ──────────────────────────────────────────────────────────

async def test_write_worker_heartbeat_sets_key_with_ttl():
    fake_redis = AsyncMock()
    with patch("backend.app.db.session.get_redis", AsyncMock(return_value=fake_redis)):
        await write_worker_heartbeat(ctx={})

    fake_redis.set.assert_called_once()
    args, kwargs = fake_redis.set.call_args
    from backend.app.core.config import WORKER_HEARTBEAT_REDIS_KEY
    assert args[0] == WORKER_HEARTBEAT_REDIS_KEY
    assert kwargs.get("ex") == WORKER_HEARTBEAT_TTL_SECONDS


async def test_write_worker_heartbeat_never_raises_on_redis_failure():
    with patch("backend.app.db.session.get_redis", AsyncMock(side_effect=ConnectionError("down"))):
        await write_worker_heartbeat(ctx={})  # must not raise — cron jobs must not crash the worker


# ── GET /ready ────────────────────────────────────────────────────────────────

class _FakeConn:
    async def execute(self, *a, **kw):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FailingConn:
    async def __aenter__(self):
        raise RuntimeError("db connection refused: password=supersecret host=10.0.0.5")

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, fail=False):
        self._fail = fail

    def connect(self):
        return _FailingConn() if self._fail else _FakeConn()


def _fake_redis(ping_ok=True, heartbeat_present=True):
    redis = AsyncMock()
    if ping_ok:
        redis.ping = AsyncMock(return_value=True)
    else:
        redis.ping = AsyncMock(side_effect=ConnectionError("redis auth failed: password=hunter2"))
    redis.get = AsyncMock(return_value="2026-09-21T00:00:00+00:00" if heartbeat_present else None)
    return redis


async def _call_ready():
    from backend.app.main import readiness
    return await readiness()


async def test_ready_all_ok_returns_200():
    with patch("backend.app.db.session.engine", _FakeEngine(fail=False)), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=_fake_redis(True, True))), \
         patch("backend.app.main.settings") as mock_settings:
        mock_settings.READY_REQUIRES_WORKER = True
        response = await _call_ready()

    assert response.status_code == 200
    import json
    body = json.loads(response.body)
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok", "worker": "ok"}


async def test_ready_db_down_returns_503_without_exception_text():
    with patch("backend.app.db.session.engine", _FakeEngine(fail=True)), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=_fake_redis(True, True))), \
         patch("backend.app.main.settings") as mock_settings:
        mock_settings.READY_REQUIRES_WORKER = True
        response = await _call_ready()

    assert response.status_code == 503
    import json
    body = json.loads(response.body)
    assert body["checks"]["database"] == "error"
    # Never leak exception text/credentials/hostnames to the caller.
    raw = response.body.decode("utf-8")
    assert "supersecret" not in raw
    assert "10.0.0.5" not in raw


async def test_ready_redis_down_returns_503_without_exception_text():
    with patch("backend.app.db.session.engine", _FakeEngine(fail=False)), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=_fake_redis(False, True))), \
         patch("backend.app.main.settings") as mock_settings:
        mock_settings.READY_REQUIRES_WORKER = True
        response = await _call_ready()

    assert response.status_code == 503
    import json
    body = json.loads(response.body)
    assert body["checks"]["redis"] == "error"
    raw = response.body.decode("utf-8")
    assert "hunter2" not in raw


async def test_ready_stale_worker_503_when_required():
    with patch("backend.app.db.session.engine", _FakeEngine(fail=False)), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=_fake_redis(True, heartbeat_present=False))), \
         patch("backend.app.main.settings") as mock_settings:
        mock_settings.READY_REQUIRES_WORKER = True
        response = await _call_ready()

    assert response.status_code == 503
    import json
    body = json.loads(response.body)
    assert body["checks"]["worker"] == "stale"


async def test_ready_stale_worker_200_when_not_required():
    with patch("backend.app.db.session.engine", _FakeEngine(fail=False)), \
         patch("backend.app.db.session.get_redis", AsyncMock(return_value=_fake_redis(True, heartbeat_present=False))), \
         patch("backend.app.main.settings") as mock_settings:
        mock_settings.READY_REQUIRES_WORKER = False
        response = await _call_ready()

    assert response.status_code == 200
    import json
    body = json.loads(response.body)
    assert body["checks"]["worker"] == "stale"
    assert body["status"] == "ok"


# ── Meta webhook: enqueue failure → 5xx; verify-token constant-time compare ──

async def test_meta_verify_webhook_wrong_token_rejected():
    from backend.app.api.v1.routes_webhook import verify_webhook

    fake_request = MagicMock()
    fake_request.url.path = "/v1/webhook/meta"
    fake_request.client.host = "203.0.113.1"

    with patch("backend.app.api.v1.routes_webhook.settings") as mock_settings:
        mock_settings.META_VERIFY_TOKEN = "the-real-token"
        response = await verify_webhook(fake_request, mode="subscribe", token="wrong-token", challenge="chal")
    assert response.status_code == 403


async def test_meta_verify_webhook_correct_token_accepted():
    from backend.app.api.v1.routes_webhook import verify_webhook

    fake_request = MagicMock()
    fake_request.url.path = "/v1/webhook/meta"
    fake_request.client.host = "203.0.113.1"

    with patch("backend.app.api.v1.routes_webhook.settings") as mock_settings:
        mock_settings.META_VERIFY_TOKEN = "the-real-token"
        response = await verify_webhook(fake_request, mode="subscribe", token="the-real-token", challenge="chal-123")
    assert response.status_code == 200
    assert response.body == b"chal-123"


async def test_meta_verify_webhook_uses_constant_time_compare():
    """hmac.compare_digest must be the comparison primitive, not `==`."""
    import backend.app.api.v1.routes_webhook as webhook_module

    with patch.object(webhook_module.hmac, "compare_digest", wraps=webhook_module.hmac.compare_digest) as spy:
        fake_request = MagicMock()
        fake_request.url.path = "/v1/webhook/meta"
        fake_request.client.host = "203.0.113.1"
        with patch("backend.app.api.v1.routes_webhook.settings") as mock_settings:
            mock_settings.META_VERIFY_TOKEN = "the-real-token"
            await webhook_module.verify_webhook(fake_request, mode="subscribe", token="the-real-token", challenge="c")
    spy.assert_called_once_with("the-real-token", "the-real-token")


async def test_webhook_post_enqueue_failure_returns_5xx():
    from backend.app.api.v1.routes_webhook import receive_webhook

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123", "display_phone_number": "+27..."},
                    "messages": [{"id": "wamid.1", "from": "27821234567", "type": "text", "text": {"body": "hi"}}],
                },
            }],
        }],
    }
    import json as jsonlib
    body = jsonlib.dumps(payload).encode("utf-8")

    fake_request = MagicMock()
    fake_request.url.path = "/v1/webhook/meta"
    fake_request.client.host = "203.0.113.1"
    fake_request.headers = {"content-type": "application/json"}
    fake_request.body = AsyncMock(return_value=body)
    fake_request.app.state.arq_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis pool down"))

    with patch("backend.app.api.v1.routes_webhook.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "development"  # signature check not under test here

        response = await receive_webhook(fake_request, x_hub_signature_256=None)

    assert response.status_code >= 500
    import json as jsonlib2
    body_out = jsonlib2.loads(response.body)
    assert body_out["status"] == "enqueue_failed"


async def test_webhook_post_success_still_returns_200():
    from backend.app.api.v1.routes_webhook import receive_webhook

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "123", "display_phone_number": "+27..."},
                    "messages": [{"id": "wamid.2", "from": "27821234567", "type": "text", "text": {"body": "hi"}}],
                },
            }],
        }],
    }
    import json as jsonlib
    body = jsonlib.dumps(payload).encode("utf-8")

    fake_request = MagicMock()
    fake_request.url.path = "/v1/webhook/meta"
    fake_request.client.host = "203.0.113.1"
    fake_request.headers = {"content-type": "application/json"}
    fake_request.body = AsyncMock(return_value=body)
    fake_request.app.state.arq_pool.enqueue_job = AsyncMock(return_value=None)

    with patch("backend.app.api.v1.routes_webhook.settings") as mock_settings:
        mock_settings.ENVIRONMENT = "development"

        result = await receive_webhook(fake_request, x_hub_signature_256=None)

    assert result == {"status": "ok"}


# ── SSE: suspended business / deactivated staff ──────────────────────────────

def _make_jwt(business_id, role="STAFF", user_id=None):
    from backend.app.core.security import create_access_token
    return create_access_token(user_id=user_id or uuid.uuid4(), business_id=business_id, role=role)


class _FakeSSEResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _FakeSSEDB:
    def __init__(self, row):
        self._row = row

    async def execute(self, stmt):
        return _FakeSSEResult(self._row)


async def test_sse_rejects_suspended_business():
    from fastapi import HTTPException

    from backend.app.realtime.sse import _get_sse_user

    business_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = _make_jwt(business_id, role="STAFF", user_id=user_id)
    db = _FakeSSEDB(row=(True, False, "Subscription unpaid"))  # user_active=True, business_active=False

    fake_request = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await _get_sse_user(fake_request, token_param=token, credentials=None, db=db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "BUSINESS_SUSPENDED"


async def test_sse_rejects_deactivated_staff():
    from fastapi import HTTPException

    from backend.app.realtime.sse import _get_sse_user

    business_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = _make_jwt(business_id, role="STAFF", user_id=user_id)
    db = _FakeSSEDB(row=(False, True, None))  # user_active=False

    fake_request = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await _get_sse_user(fake_request, token_param=token, credentials=None, db=db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ACCOUNT_DISABLED"


async def test_sse_allows_active_user_and_business():
    from backend.app.realtime.sse import _get_sse_user

    business_id = uuid.uuid4()
    user_id = uuid.uuid4()
    token = _make_jwt(business_id, role="MANAGER", user_id=user_id)
    db = _FakeSSEDB(row=(True, True, None))

    fake_request = MagicMock()

    user = await _get_sse_user(fake_request, token_param=token, credentials=None, db=db)
    assert user.business_id == business_id
    assert user.role == "MANAGER"
