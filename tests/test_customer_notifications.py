"""
Tests for _customer_status_message — the fix for Issue 2.

Verifies that every order status transition that should notify the customer
produces the correct message text, and that status changes that should NOT
notify (e.g. IN_PROGRESS) are handled correctly.
"""

import pytest

from backend.app.api.v1.routes_orders import _customer_status_message


class TestCustomerStatusMessage:

    def test_accepted_no_eta(self):
        msg = _customer_status_message("ACCEPTED", "NG-001", None, None)
        assert msg is not None
        assert "NG-001" in msg
        assert "accepted" in msg.lower()
        # No ETA line when not provided
        assert "minutes" not in msg

    def test_accepted_with_eta(self):
        msg = _customer_status_message("ACCEPTED", "NG-001", 20, None)
        assert msg is not None
        assert "20 minutes" in msg

    def test_in_progress(self):
        msg = _customer_status_message("IN_PROGRESS", "NG-002", None, None)
        assert msg is not None
        assert "NG-002" in msg
        assert "prepared" in msg.lower() or "progress" in msg.lower()

    def test_ready(self):
        msg = _customer_status_message("READY", "NG-003", None, None)
        assert msg is not None
        assert "NG-003" in msg
        # Must signal the order is ready
        assert any(word in msg.lower() for word in ["ready", "collect", "pickup"])

    def test_delivered(self):
        msg = _customer_status_message("DELIVERED", "NG-004", None, None)
        assert msg is not None
        assert "NG-004" in msg
        assert "delivered" in msg.lower()

    def test_collected(self):
        msg = _customer_status_message("COLLECTED", "NG-005", None, None)
        assert msg is not None
        assert "NG-005" in msg

    def test_cancelled_no_reason(self):
        msg = _customer_status_message("CANCELLED", "NG-006", None, None)
        assert msg is not None
        assert "NG-006" in msg
        assert "cancel" in msg.lower()
        # No reason line when not provided
        assert "Reason" not in msg

    def test_cancelled_with_reason(self):
        msg = _customer_status_message("CANCELLED", "NG-007", None, "Out of stock")
        assert msg is not None
        assert "NG-007" in msg
        assert "Out of stock" in msg

    def test_unknown_status_returns_none(self):
        """Statuses with no customer message (e.g. intermediate internal states)
        should return None so the caller knows not to send anything."""
        msg = _customer_status_message("NEW", "NG-008", None, None)
        assert msg is None

    @pytest.mark.parametrize("status", [
        "ACCEPTED", "IN_PROGRESS", "READY", "DELIVERED", "COLLECTED", "CANCELLED",
    ])
    def test_all_notifiable_statuses_include_order_number(self, status):
        order_number = "NG-999"
        msg = _customer_status_message(status, order_number, None, None)
        assert msg is not None, f"Status {status} should produce a message"
        assert order_number in msg, f"Order number missing from {status} message"
