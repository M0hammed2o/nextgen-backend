"""
Server-side trading gate for event vendors (souk).

A business on the event plan may only create a real sale or record money
while it holds a PAID subscription covering the current trading date at one of
its events (access level TRADE, rules.py). The level is computed live from the
database on every call, never from a cached flag, and "today" is the event's
own timezone.

Non-event businesses (any plan other than EVENT_PLAN) are never affected.

NOT applied to WhatsApp-originated orders: the protected bot pipeline files
(pipeline.py, order_creator.py, ...) are out of scope for the change that added
this gate, so a bot order is NOT checked here. The earlier assumption that
event vendors "have no WhatsApp number, so that channel is unreachable" was
false (security review H1): an event vendor's owner could connect their own
number through Embedded Signup. The interim mitigation is therefore:
  - every /business/whatsapp/connection mutation is refused for event-plan
    businesses (403 EVENT_WHATSAPP_NOT_AVAILABLE);
  - event vendors are provisioned with is_whatsapp_enabled = false, and
    migration 0065 switched it off for existing event-plan businesses.
OWNER APPROVAL NEEDED: gate bot order creation for event vendors (call
assert_trading_allowed from the bot's order-creation path, which means editing
the protected files). Until then WhatsApp must stay off for event vendors.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.events.rules import TRADE
from backend.app.events.service import business_access_decision
from shared.enums import EVENT_PLAN
from shared.models.business import Business

_MESSAGES = {
    "NO_PAID_COVER_TODAY": "Trading is not allowed: there is no paid event subscription covering today.",
    "NOT_AN_EVENT_DAY": "Trading is not allowed: today is not an event trading day.",
    "CALENDAR_NOT_PUBLISHED": "Trading is not allowed: the event calendar has not been published yet.",
    "PREP_WINDOW": "Trading is not allowed yet: the event has not started. You can configure your stall.",
}


async def assert_trading_allowed(db: AsyncSession, business_id: uuid.UUID, now: datetime | None = None) -> None:
    """Raise 403 EVENT_TRADING_NOT_ALLOWED unless an event-plan business may trade right now."""
    plan = (await db.execute(select(Business.plan).where(Business.id == business_id))).scalar_one_or_none()
    if plan != EVENT_PLAN:
        return
    decision = await business_access_decision(db, business_id, now)
    if decision.level == TRADE:
        return
    raise AppError(
        "EVENT_TRADING_NOT_ALLOWED",
        _MESSAGES.get(decision.reason, "Trading is not allowed for this event vendor right now."),
        403,
        details={"reason": decision.reason, "access_level": decision.level},
    )
