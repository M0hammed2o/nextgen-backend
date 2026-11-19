"""
Offline POS sync helpers (Phase 3b): attribute a queued sale to the till
session that was open when it was rung up, and detect stock shortfalls.

Pure persistence helpers; the route owns the transaction and the commit.
Every query is filtered by a business_id that came from the caller's JWT.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.till import TillSession


async def find_till_open_at(
    db: AsyncSession, business_id: uuid.UUID, captured_at: datetime,
) -> TillSession | None:
    """
    The till session of this business that covered `captured_at`:
    opened_at <= captured_at and (still open, or closed_at >= captured_at).

    One-till-per-business means sessions never overlap; if clock oddities ever
    produced two matches the most recently opened wins. If the match is still
    OPEN it is re-read FOR UPDATE, so the sale cannot slip into a session a
    concurrent close_till is in the middle of totalling (same rule as
    _get_open_till). A CLOSED session is immutable and needs no lock.
    Returns None when nothing covers the instant: the caller must then leave
    the sale unattributed and flag it, never fall back to "whatever is open now".
    """
    query = (
        select(TillSession)
        .where(
            TillSession.business_id == business_id,
            TillSession.opened_at <= captured_at,
            or_(TillSession.closed_at.is_(None), TillSession.closed_at >= captured_at),
        )
        .order_by(TillSession.opened_at.desc())
        .limit(1)
    )
    session = (await db.execute(query)).scalars().first()
    if session is not None and session.status == "OPEN":
        session = (
            await db.execute(
                select(TillSession)
                .where(TillSession.id == session.id, TillSession.business_id == business_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        # A close may have committed between the two reads; the locked row is the truth.
        if session is not None and session.closed_at is not None and session.closed_at < captured_at:
            return None
    return session
