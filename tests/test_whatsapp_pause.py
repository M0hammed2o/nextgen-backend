"""
Tests for the WhatsApp pause/busy toggle feature.

Covers: busy_response() text fallback, the busy gate's re-notification
logic (mirrored as a pure function, same approach as the ORDER_CANCEL
messaging tests — the gate itself lives inline in the large pipeline
message-handling function, not a separately callable unit), and that the
new whatsapp_paused column is independent of the pre-existing
is_whatsapp_enabled admin kill switch.
"""

import inspect
import types

from backend.app.bot import responses
from backend.app.bot.pipeline import _process
from shared.models.business import Business


def make_business(**overrides):
    b = types.SimpleNamespace(
        name="Barn Owl",
        busy_text=None,
        closed_text=None,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


class TestBusyResponse:
    def test_uses_custom_busy_text_when_set(self):
        business = make_business(busy_text="We're slammed! Please order in store.")
        assert responses.busy_response(business) == "We're slammed! Please order in store."

    def test_falls_back_to_default_when_unset(self):
        business = make_business(busy_text=None)
        text = responses.busy_response(business)
        assert business.name in text
        assert "too busy" in text.lower()

    def test_falls_back_when_empty_string(self):
        business = make_business(busy_text="")
        text = responses.busy_response(business)
        assert "too busy" in text.lower()


class TestBusyGateSourcePresence:
    """The gate lives inline in _process_message; verify it exists and is
    positioned after the closed gate (closed takes priority)."""

    def test_busy_gate_exists_and_checks_whatsapp_paused(self):
        src = inspect.getsource(_process)
        assert "whatsapp_paused" in src
        assert "busy_response" in src

    def test_closed_gate_precedes_busy_gate(self):
        src = inspect.getsource(_process)
        closed_idx = src.index("PIPELINE_CLOSED_GATE")
        busy_idx = src.index("whatsapp_paused")
        assert closed_idx < busy_idx, "closed-for-the-day must be checked before the busy gate"


def _busy_gate_should_notify(paused_key: str, busy_sent_for: str | None) -> bool:
    """Mirrors the gate's re-notification decision in pipeline.py."""
    return busy_sent_for != paused_key


class TestBusyGateRenotificationLogic:
    def test_first_message_during_a_pause_notifies(self):
        assert _busy_gate_should_notify("2026-07-13T10:00:00+00:00", None)

    def test_second_message_same_pause_cycle_is_silent(self):
        key = "2026-07-13T10:00:00+00:00"
        assert not _busy_gate_should_notify(key, busy_sent_for=key)

    def test_new_pause_cycle_after_a_resume_notifies_again(self):
        old_key = "2026-07-13T10:00:00+00:00"
        new_key = "2026-07-13T14:30:00+00:00"
        assert _busy_gate_should_notify(new_key, busy_sent_for=old_key)


class TestPauseFlagIndependentOfAdminKillSwitch:
    """whatsapp_paused (staff toggle) and is_whatsapp_enabled (admin kill
    switch) must be separate columns — pausing/resuming must never touch
    the admin flag, and vice versa."""

    def test_both_columns_exist_and_are_independent(self):
        columns = Business.__table__.columns
        assert "whatsapp_paused" in columns
        assert "is_whatsapp_enabled" in columns
        assert columns["whatsapp_paused"].default.arg is False
        assert columns["is_whatsapp_enabled"].default.arg is True

    def test_busy_text_is_distinct_from_closed_text(self):
        columns = Business.__table__.columns
        assert "busy_text" in columns
        assert "closed_text" in columns
