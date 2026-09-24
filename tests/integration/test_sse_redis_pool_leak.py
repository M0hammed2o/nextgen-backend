"""
Real, non-mocked proof that backend/app/realtime/sse.py returns its Redis
pubsub connection to the shared pool even under genuine ASGI-level
cancellation — regression test for KNOWN_BUGS.md BUG-0110.

WHY A MOCK CANNOT PROVE THIS: the leak was never about redis-py's API
surface (a mock would happily call `.close()` if asked) — it was about
*whether cleanup code gets a chance to run at all* once
sse_starlette.sse.EventSourceResponse.__call__ (real code, imported
directly, not reimplemented here) calls `task_group.cancel_scope.cancel()`
on client disconnect. anyio's structured-concurrency cancellation is NOT a
single `asyncio.Task.cancel()`: every checkpoint (every `await`) reached
while still inside the cancelled scope raises the cancellation exception
again, including ones inside a `finally` block — this only happens with a
real anyio task group actually cancelling a real coroutine mid-await, which
is exactly what this test drives.

Method: constructs a genuine `starlette.requests.Request` and a fake-but-
ASGI-conformant `receive` callable that reports the client is present, then
(after a short delay) reports `http.disconnect` — the same message shape a
real ASGI server sends when a browser tab closes. Feeds both into the REAL
`EventSourceResponse.__call__` returned by the actual
`live_order_stream()` route function (called directly, not through FastAPI
routing/dependency injection — `Depends(...)` is just a default parameter
value when the function is called as plain Python, so `user=` is supplied
directly). This exercises the identical anyio cancellation delivery the
production bug hit, end to end, with zero mocking of the cancellation
mechanism itself.

Real Redis only (backend/app/db/session.py's `_redis_pool` singleton is
seeded, once, with a real client bound to the dedicated TEST_REDIS_URL, via
the standard `monkeypatch` fixture so it's automatically restored after
this test — never TEST_DATABASE_URL: this specific route never touches
Postgres once past its (separately, already-tested — see
tests/test_worker_readiness.py's `test_sse_*` cases) auth dependency, so
this file intentionally has no `db`/`test_business` fixture dependency).
Uses ONLY the dedicated tests/integration environment
(docker-compose.test.yml, TEST_REDIS_URL — see tests/integration/_env.py
and TESTING.md); never any other Redis instance.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import redis.asyncio as aioredis
from starlette.requests import Request

import backend.app.db.session as db_session_module
from backend.app.core.config import WORKER_HEARTBEAT_REDIS_KEY
from backend.app.core.rbac import AuthUser
from backend.app.realtime.sse import live_order_stream
from backend.app.worker import WORKER_HEARTBEAT_TTL_SECONDS
from tests.integration._env import get_test_redis_url

# The original production default (backend/app/db/session.py, before this
# fix) — deliberately reproduced here rather than the new, larger default,
# so this test proves the fix works at the exact size the real incident
# happened at, independent of the separate pool-sizing judgement call.
_ORIGINAL_MAX_CONNECTIONS = 20


def _fake_disconnecting_receive(disconnect_after: float):
    """
    ASGI `receive` callable: one `http.request` immediately, then
    `http.disconnect` once `disconnect_after` seconds have elapsed (and on
    every call after that — a real ASGI server keeps reporting the
    connection as gone once it notices). `disconnect_after` is kept short
    (tens of milliseconds) so the disconnect is very likely delivered while
    the generator is genuinely suspended inside a redis await (`pubsub.get_
    message(timeout=1.0)`), matching what the measured production evidence
    implies about the race (unsubscribe() being cancelled mid-await, not
    the generator's own polled `is_disconnected()` check winning first).
    """
    state = {"sent_initial": False, "t0": None}

    async def _receive():
        if state["t0"] is None:
            state["t0"] = time.monotonic()
        if not state["sent_initial"]:
            state["sent_initial"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        remaining = disconnect_after - (time.monotonic() - state["t0"])
        if remaining > 0:
            await asyncio.sleep(remaining)
        return {"type": "http.disconnect"}

    return _receive


async def _noop_send(message: dict) -> None:
    return None


def _make_request(receive) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/v1/business/orders/live/stream",
        "raw_path": b"/v1/business/orders/live/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
    }
    return Request(scope, receive=receive)


@pytest.fixture
async def real_redis_singleton(monkeypatch):
    """
    Seeds backend/app/db/session.py's module-level `_redis_pool` singleton
    with a REAL redis.asyncio client bound to TEST_REDIS_URL, built with
    the same `BlockingConnectionPool`/`max_connections=20` shape production
    used at the time this bug was found. Every caller of the real (entirely
    unmodified) `get_redis()` — including `backend/app/realtime/sse.py`'s
    own already-bound reference and `backend/app/main.py::readiness()`'s
    fresh per-call import — reads this same module-global, so seeding it
    here (rather than monkeypatching the `get_redis` name in two separate
    modules) exercises the real function body in both places.
    `monkeypatch` auto-restores the attribute after the test, so this can
    never leak into another test file's process.
    """
    pool = aioredis.BlockingConnectionPool.from_url(
        get_test_redis_url(),
        decode_responses=True,
        max_connections=_ORIGINAL_MAX_CONNECTIONS,
        timeout=3.0,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )
    client = aioredis.Redis(connection_pool=pool)
    await client.ping()  # fail loudly here, not mid-test, if TEST_REDIS_URL is unreachable

    monkeypatch.setattr(db_session_module, "_redis_pool", client)

    yield client, pool

    await client.aclose()


async def test_more_than_20_abandoned_sse_streams_leave_the_pool_and_ready_healthy(
    real_redis_singleton,
):
    """
    Opens and genuinely cancels 25 SSE streams (more than the original
    max_connections=20) via the real EventSourceResponse/anyio cancellation
    path. Before this fix: stream #20 exhausted the pool and every /ready
    check after it returned 503 forever (verified via the pre-fix
    reproduction — see KNOWN_BUGS.md BUG-0110 and the Playwright spec at
    e2e-tests/tests/rc0051/99-known-defect-sse-redis-leak.spec.ts, which
    drives the same scenario against a real running process). After this
    fix: the pool and /ready must both stay healthy through every single
    one of the 25 streams, not just "eventually recover".
    """
    client, pool = real_redis_singleton

    # A genuinely alive worker heartbeat, matching the real incident (the
    # worker was verified alive and heartbeating throughout — only /ready's
    # OWN redis read was starved by the leak, a second phantom symptom of
    # the same single bug, not a real worker outage).
    await client.set(WORKER_HEARTBEAT_REDIS_KEY, "alive", ex=WORKER_HEARTBEAT_TTL_SECONDS)

    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="MANAGER", token_type="access")

    trail: list[str] = []
    for i in range(1, 26):
        receive = _fake_disconnecting_receive(disconnect_after=0.05)
        response = await live_order_stream(_make_request(receive), user=user)

        # Drives the REAL sse_starlette ASGI __call__ — this is what
        # actually opens the anyio task group and calls
        # `task_group.cancel_scope.cancel()` on disconnect, the exact
        # mechanism that leaked connections in production.
        await asyncio.wait_for(
            response({"type": "http"}, receive, _noop_send),
            timeout=5.0,
        )

        # /ready's own primitive, exercised directly against the real pool
        # after EVERY single abandoned stream — proves the pool never even
        # transiently degrades, not just that it recovers eventually.
        ping_ok = True
        try:
            await asyncio.wait_for(client.ping(), timeout=2.0)
        except Exception as exc:  # noqa: BLE001 — recording the failure is the point
            ping_ok = False
            trail.append(f"stream #{i}: PING FAILED: {exc!r}")
        else:
            trail.append(f"stream #{i}: ping ok")

        assert ping_ok, "redis pool degraded after stream #%d abandoned; trail:\n%s" % (i, "\n".join(trail))

    # The pool's own bookkeeping: nothing left checked out once every
    # stream above has been opened and abandoned.
    in_use = getattr(pool, "_in_use_connections", None)
    assert in_use is not None, "BlockingConnectionPool internals changed shape; update this assertion"
    assert len(in_use) == 0, f"connections still checked out after cleanup: {in_use}"

    # The literal assertion this bug report specifies: /ready must return
    # 200 after this. Calls the REAL readiness() from backend/app/main.py
    # (not reimplemented) — only its DB check is faked (this test is about
    # the Redis pool, not Postgres; the DB portion of /ready has its own
    # coverage in tests/test_worker_readiness.py), everything Redis-related
    # is the real pool exercised above.
    from unittest.mock import patch

    from backend.app.main import readiness

    class _OkConn:
        async def execute(self, *a, **kw):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _OkEngine:
        def connect(self):
            return _OkConn()

    with patch.object(db_session_module, "engine", _OkEngine()):
        response = await readiness()

    import json as jsonlib

    body = jsonlib.loads(response.body)
    assert response.status_code == 200, f"/ready degraded after {25} abandoned SSE streams: {body}"
    assert body == {"status": "ok", "checks": {"database": "ok", "redis": "ok", "worker": "ok"}}
