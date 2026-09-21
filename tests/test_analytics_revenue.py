"""
Unit tests for the pure predicate helpers in backend/app/analytics/revenue.py
(BUG-0043). No database required — these mirror the SQL predicates used by
every analytics endpoint, so they double as executable documentation of the
sale/void/cancellation definition.

See tests/integration/test_analytics_revenue.py for the real-DB tests that
prove the SQL expressions built from the same tuples produce the same
answers when aggregated by Postgres.
"""
from backend.app.analytics.revenue import (
    order_is_sale,
    order_is_true_cancellation,
    order_is_voided,
)


class TestOrderIsSale:
    # ── POS till sales: created already PAID, fulfilment status varies ──────
    def test_pos_grab_and_go_completed_paid_is_a_sale(self):
        assert order_is_sale(status="COMPLETED", payment_status="PAID") is True

    def test_pos_needing_kitchen_prep_accepted_paid_is_a_sale(self):
        # The exact defect BUG-0043 describes: paid but not yet
        # COLLECTED/DELIVERED.
        assert order_is_sale(status="ACCEPTED", payment_status="PAID") is True

    def test_pos_new_unpaid_is_not_a_sale(self):
        assert order_is_sale(status="NEW", payment_status="UNPAID") is False

    # ── WhatsApp cash-on-collection: payment_status never becomes PAID ──────
    def test_whatsapp_cash_on_collection_collected_is_a_sale(self):
        assert order_is_sale(status="COLLECTED", payment_status="CASH_ON_COLLECTION") is True

    def test_whatsapp_cash_on_collection_delivered_is_a_sale(self):
        assert order_is_sale(status="DELIVERED", payment_status="CASH_ON_COLLECTION") is True

    def test_whatsapp_order_still_in_funnel_unpaid_is_not_a_sale(self):
        assert order_is_sale(status="NEW", payment_status="UNPAID") is False
        assert order_is_sale(status="ACCEPTED", payment_status="PENDING") is False

    # ── Imported marketplace orders: created COMPLETED + PAID ───────────────
    def test_imported_order_is_a_sale(self):
        assert order_is_sale(status="COMPLETED", payment_status="PAID") is True

    # ── Refunds ──────────────────────────────────────────────────────────
    def test_fully_refunded_order_still_counts_as_a_sale(self):
        # Stays a sale (net 0) rather than disappearing from order counts.
        assert order_is_sale(status="ACCEPTED", payment_status="REFUNDED") is True

    def test_partially_refunded_order_still_counts_as_a_sale(self):
        assert order_is_sale(status="COLLECTED", payment_status="PARTIALLY_REFUNDED") is True

    # ── Cancellation always wins, regardless of payment_status ──────────────
    def test_cancelled_order_is_never_a_sale_even_if_paid(self):
        assert order_is_sale(status="CANCELLED", payment_status="PAID") is False

    def test_voided_order_is_not_a_sale(self):
        # A void always also sets status=CANCELLED (see routes_pos.py
        # void_pos_order()), so this is already covered by the CANCELLED
        # check above, but assert the exact real-world combination too.
        assert order_is_sale(status="CANCELLED", payment_status="VOIDED") is False


class TestOrderIsVoided:
    def test_voided_payment_status_is_voided(self):
        assert order_is_voided(payment_status="VOIDED") is True

    def test_refunded_is_not_voided(self):
        assert order_is_voided(payment_status="REFUNDED") is False

    def test_paid_is_not_voided(self):
        assert order_is_voided(payment_status="PAID") is False


class TestOrderIsTrueCancellation:
    def test_cancelled_never_paid_is_a_true_cancellation(self):
        assert order_is_true_cancellation(status="CANCELLED", payment_status="UNPAID") is True

    def test_voided_pos_sale_is_not_a_true_cancellation(self):
        # A void is a distinct operational event from an order that was
        # simply never accepted/paid -- see routes_pos.py void_pos_order().
        assert order_is_true_cancellation(status="CANCELLED", payment_status="VOIDED") is False

    def test_active_order_is_not_a_cancellation(self):
        assert order_is_true_cancellation(status="ACCEPTED", payment_status="PAID") is False

    def test_every_cancelled_order_is_exactly_one_of_true_cancellation_or_voided(self):
        for payment_status in (
            "UNPAID", "PENDING", "PAID", "PARTIALLY_REFUNDED", "REFUNDED", "FAILED",
            "VOIDED", "CASH_ON_COLLECTION",
        ):
            is_true_cancel = order_is_true_cancellation(status="CANCELLED", payment_status=payment_status)
            is_void = order_is_voided(payment_status=payment_status)
            assert is_true_cancel != is_void, (
                f"payment_status={payment_status!r} on a CANCELLED order must be "
                "classified as exactly one of true-cancellation or voided"
            )
