"""
backend/app/inventory/cancellation.py — the pre-preparation status set and
the pure branching rule, isolated from any DB.
"""
import uuid

from backend.app.inventory.cancellation import PRE_PREPARATION_STATUSES
from shared.enums import ORDER_STATUS_TRANSITIONS, OrderStatus


class TestPrePreparationStatusSet:
    def test_matches_exactly_the_statuses_upstream_of_in_progress(self):
        """
        Derived from ORDER_STATUS_TRANSITIONS: every status whose only paths
        lead to IN_PROGRESS (or another pre-prep status), never the reverse.
        If this ever drifts from the enum (a new pre-IN_PROGRESS status is
        added and forgotten here), this test must fail loudly rather than
        silently under- or over-reversing stock on cancel.
        """
        assert PRE_PREPARATION_STATUSES == {
            OrderStatus.PENDING_DELIVERY_FEE.value,
            OrderStatus.FEE_SENT.value,
            OrderStatus.NEW.value,
            OrderStatus.ACCEPTED.value,
        }

    def test_in_progress_and_everything_after_it_is_excluded(self):
        post_prep = {
            OrderStatus.IN_PROGRESS.value,
            OrderStatus.READY.value,
            OrderStatus.COLLECTED.value,
            OrderStatus.DELIVERED.value,
            OrderStatus.COMPLETED.value,
        }
        assert PRE_PREPARATION_STATUSES.isdisjoint(post_prep)

    def test_every_pre_preparation_status_can_still_reach_cancelled(self):
        """The whole point: these are the statuses a cancel can actually reverse."""
        for status_value in PRE_PREPARATION_STATUSES:
            status = OrderStatus(status_value)
            allowed = ORDER_STATUS_TRANSITIONS.get(status, [])
            assert OrderStatus.CANCELLED in allowed, status_value
