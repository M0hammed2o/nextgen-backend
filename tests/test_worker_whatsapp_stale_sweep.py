"""
Gate hardening pass, 2026-09-22 — §2 failure-mode sweep: observability-only
daily cron (backend/app/worker.py::log_stale_whatsapp_connections) that logs
a CONNECTED whatsapp_connections row that's gone quiet for too long.
Deliberately does NOT mutate `status`/`status_reason` — see that function's
own docstring for why. This file proves both halves: it DOES log for a
genuinely stale CONNECTED row, and it does NOT touch (log or mutate) a
row that's either not stale or not CONNECTED.

Real throwaway DB (skips cleanly if unreachable, same pattern as the rest of
this worktree's WhatsApp tests).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.session import async_session_factory, engine
from backend.app.worker import STALE_WHATSAPP_WEBHOOK_DAYS, log_stale_whatsapp_connections
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db() -> AsyncSession:
    if not await _db_reachable():
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping.")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


async def _make_connected_business_with_connection(
    db: AsyncSession, *, last_webhook_at, status="CONNECTED",
) -> tuple[Business, WhatsAppConnection]:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Stale Sweep Test {unique}",
        slug=f"stale-sweep-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
    )
    db.add(business)
    await db.flush()

    connection = WhatsAppConnection(
        business_id=business.id,
        waba_id=f"waba-{unique}",
        phone_number_id=f"phone-{unique}",
        status=status,
        last_webhook_at=last_webhook_at,
    )
    db.add(connection)
    await db.commit()
    await db.refresh(business)
    await db.refresh(connection)
    return business, connection


class _FakeCtx(dict):
    """ctx["session_factory"]() must be an async context manager, like the real ARQ ctx."""


async def test_stale_connected_connection_is_logged_not_mutated(db, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="nextgen.worker")

    stale_at = datetime.now(timezone.utc) - timedelta(days=STALE_WHATSAPP_WEBHOOK_DAYS + 1)
    business, connection = await _make_connected_business_with_connection(db, last_webhook_at=stale_at)

    ctx = _FakeCtx(session_factory=async_session_factory)
    await log_stale_whatsapp_connections(ctx)

    assert any(
        "WHATSAPP_CONNECTION_STALE" in record.message and str(business.id) in record.message
        for record in caplog.records
    ), "expected a WHATSAPP_CONNECTION_STALE log line naming this business"

    # Deliberately unmutated — see function docstring.
    await db.refresh(connection)
    assert connection.status == "CONNECTED"
    assert connection.status_reason is None


async def test_recently_active_connected_connection_is_not_logged(db, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="nextgen.worker")

    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    business, connection = await _make_connected_business_with_connection(db, last_webhook_at=recent)

    ctx = _FakeCtx(session_factory=async_session_factory)
    await log_stale_whatsapp_connections(ctx)

    assert not any(str(business.id) in record.message for record in caplog.records)


async def test_stale_but_not_connected_connection_is_not_logged(db, caplog):
    """
    Only CONNECTED rows are in scope — a NEEDS_ATTENTION/DISCONNECTED row is
    already surfaced through its own status/status_reason, and Meta going
    quiet on an already-non-CONNECTED row tells an operator nothing new.
    """
    import logging
    caplog.set_level(logging.WARNING, logger="nextgen.worker")

    stale_at = datetime.now(timezone.utc) - timedelta(days=STALE_WHATSAPP_WEBHOOK_DAYS + 5)
    business, connection = await _make_connected_business_with_connection(
        db, last_webhook_at=stale_at, status="NEEDS_ATTENTION",
    )

    ctx = _FakeCtx(session_factory=async_session_factory)
    await log_stale_whatsapp_connections(ctx)

    assert not any(str(business.id) in record.message for record in caplog.records)


async def test_never_received_a_webhook_at_all_counts_as_stale(db, caplog):
    """A CONNECTED row with last_webhook_at still NULL (never heard from Meta at all) must also be flagged."""
    import logging
    caplog.set_level(logging.WARNING, logger="nextgen.worker")

    business, connection = await _make_connected_business_with_connection(db, last_webhook_at=None)

    ctx = _FakeCtx(session_factory=async_session_factory)
    await log_stale_whatsapp_connections(ctx)

    assert any(
        "WHATSAPP_CONNECTION_STALE" in record.message and str(business.id) in record.message
        for record in caplog.records
    )
