"""
Real-Postgres check that a 100-vendor bulk onboarding (the cap) does not starve the
rest of the admin API while it runs.

Each new vendor's owner gets a bcrypt-hashed temporary password (~0.6-1 s of CPU per
hash). Hashing them all at once through ``run_in_threadpool`` used every token of
Starlette/anyio's shared threadpool (40 by default) for as long as the hashing took,
so anything else in the process that needs that pool (sync endpoints and
dependencies, file responses, other ``run_in_threadpool`` calls) queued behind it,
and 40 CPU-bound threads crowded the event loop off the CPU.

While the real route function imports 100 vendors against the real database, this
test concurrently probes:
  * ``GET /health`` on the real admin app (in-process, ASGI): any async endpoint;
  * a no-op ``run_in_threadpool`` call: what any sync endpoint/dependency would wait;
and records how many hashes ran at once and how many SQL statements the import ran.
Numbers are printed (``-s`` not needed); the assertions are the bounded hash
concurrency and that the shared threadpool stays available (generous bound, since
the CPU is shared with other work on a test machine).
"""
from __future__ import annotations

import asyncio
import statistics
import threading
import time

import httpx
import pytest
from sqlalchemy import event
from starlette.concurrency import run_in_threadpool

from admin_api.app.api.v1 import routes_events as admin_events
from admin_api.app.api.v1.routes_events import BulkVendorRequest, bulk_onboard_vendors
from admin_api.app.services import provisioning
from backend.app.core import security
from tests.integration.test_event_scale_lifecycle_realdb import _event, _row, clock  # noqa: F401
from tests.integration.test_souk_security_review_realdb import ADMIN, bag  # noqa: F401

pytestmark = pytest.mark.asyncio


def _stats(samples: list[float]) -> str:
    if not samples:
        return "no samples"
    s = sorted(samples)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return (f"n={len(s)} p50={statistics.median(s) * 1000:.0f}ms p95={p95 * 1000:.0f}ms "
            f"max={s[-1] * 1000:.0f}ms")


async def test_100_vendor_import_keeps_the_admin_api_responsive(db, bag, clock, monkeypatch, capsys):
    from admin_api.app.main import app as admin_app

    ev = await _event(db, bag)
    rows = [_row(bag, "FOOD" if i % 2 else "CLOTHING", stall=f"R-{i:03d}") for i in range(100)]

    # Count concurrent bcrypt hashes (wrapping the real function: real cost, real hashes).
    real_hash = security.hash_password
    lock = threading.Lock()
    live = {"now": 0, "max": 0, "calls": 0, "last_end": 0.0}

    def counting_hash(pw: str) -> str:
        with lock:
            live["now"] += 1
            live["calls"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            return real_hash(pw)
        finally:
            with lock:
                live["now"] -= 1
                live["last_end"] = time.perf_counter()

    monkeypatch.setattr(provisioning, "hash_password", counting_hash)
    if hasattr(admin_events, "hash_password"):  # the pre-fix route hashed directly
        monkeypatch.setattr(admin_events, "hash_password", counting_hash)

    statements = {"n": 0}

    def _count(*_a):
        statements["n"] += 1

    sync_engine = (await db.connection()).engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _count)

    health_lat: list[float] = []
    pool_lat: list[float] = []
    done = asyncio.Event()

    async def probe_health(client: httpx.AsyncClient):
        while not done.is_set():
            t0 = time.perf_counter()
            r = await client.get("/health")
            health_lat.append(time.perf_counter() - t0)
            assert r.status_code == 200
            await asyncio.sleep(0.1)

    async def probe_pool():
        while not done.is_set():
            t0 = time.perf_counter()
            await run_in_threadpool(lambda: None)
            pool_lat.append(time.perf_counter() - t0)
            await asyncio.sleep(0.1)

    async def run_import():
        try:
            t0 = live["start"] = time.perf_counter()
            res = await bulk_onboard_vendors(ev["id"], BulkVendorRequest(vendors=rows), ADMIN, db)
            return res, time.perf_counter() - t0
        finally:
            done.set()

    # Baseline: the same probes with nothing else running.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://admin") as client:
        idle_h, idle_p = [], []
        for _ in range(10):
            t0 = time.perf_counter()
            assert (await client.get("/health")).status_code == 200
            idle_h.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            await run_in_threadpool(lambda: None)
            idle_p.append(time.perf_counter() - t0)

        try:
            (res, elapsed), _, _ = await asyncio.gather(run_import(), probe_health(client), probe_pool())
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count)

    with capsys.disabled():
        print(f"\n[responsiveness] 100-vendor import wall time: {elapsed:.2f}s; "
              f"hashes={live['calls']} max concurrent={live['max']} "
              f"(hashing done at +{live['last_end'] - live['start']:.2f}s); SQL statements={statements['n']}")
        print(f"[responsiveness] idle    /health {_stats(idle_h)} | threadpool {_stats(idle_p)}")
        print(f"[responsiveness] import  /health {_stats(health_lat)} | threadpool {_stats(pool_lat)}")

    vs = res["vendors"]
    assert len(vs) == 100 and len({v["owner_temporary_password"] for v in vs}) == 100
    assert live["calls"] == 100
    limit = getattr(provisioning, "BCRYPT_MAX_CONCURRENCY", None)
    assert limit is not None and live["max"] <= limit, (live["max"], limit)
    # The shared threadpool is never exhausted by the import: a no-op call gets a thread at once.
    assert max(pool_lat) < 1.0, _stats(pool_lat)
