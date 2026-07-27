"""
Shared billing-lifecycle transitions — reused by every billing provider's
webhook handler (Stripe's `_handle_invoice_paid`/`_handle_payment_failed`,
PayFast's ITN handler) so the actual state-machine logic for
"a payment succeeded" / "a payment failed" lives in exactly one place,
regardless of which provider is active.
"""

import logging
from datetime import datetime, timedelta, timezone

from shared.models.billing_invoice import BillingInvoice
from shared.models.business import Business

logger = logging.getLogger("nextgen.billing.lifecycle")

# How long a business stays PAST_DUE (retried) before being suspended.
# "Retry. Notify. Grace period. Suspend account." — this is the grace period.
GRACE_PERIOD_DAYS = 7


def mark_invoice_paid(
    business: Business, db=None, amount_cents: int | None = None,
    provider: str | None = None, provider_reference: str | None = None,
) -> None:
    """
    A billing-period invoice was paid — (re)activate and start a fresh
    period. When `db` is provided, also records a `BillingInvoice` row
    (invoice history — "Store invoice history").
    """
    was_past_due = business.billing_status == "PAST_DUE"
    business.billing_status = "ACTIVE"
    business.is_active = True
    business.suspended_reason = None
    business.billing_period_started_at = datetime.now(timezone.utc)
    if was_past_due:
        logger.info("Business %s reactivated after payment", business.id)

    if db is not None and amount_cents is not None:
        db.add(BillingInvoice(
            business_id=business.id, provider=provider or "UNKNOWN", provider_reference=provider_reference,
            amount_cents=amount_cents, status="PAID", plan=business.plan,
        ))


def mark_payment_failed(
    business: Business, db=None, amount_cents: int | None = None,
    provider: str | None = None, provider_reference: str | None = None,
) -> None:
    """
    A billing-period payment failed. Starts (or continues) the grace period —
    does NOT suspend immediately. `is_past_grace_period` decides suspension.
    """
    business.billing_status = "PAST_DUE"
    logger.info("Business %s marked PAST_DUE after a failed billing payment", business.id)

    if db is not None and amount_cents is not None:
        db.add(BillingInvoice(
            business_id=business.id, provider=provider or "UNKNOWN", provider_reference=provider_reference,
            amount_cents=amount_cents, status="FAILED", plan=business.plan,
        ))


def is_past_grace_period(business: Business, grace_period_days: int = GRACE_PERIOD_DAYS) -> bool:
    """
    Whether a PAST_DUE business has exceeded its grace period and should be
    suspended. Grace is measured from the start of the current billing
    period (the last successful payment) — the only reliably-known
    "since when has this been unpaid" anchor available without a dedicated
    "first failed payment at" timestamp (a reasonable future refinement,
    not built this pass — see TODO.md).
    """
    if business.billing_status != "PAST_DUE":
        return False
    deadline = business.billing_period_started_at + timedelta(days=grace_period_days)
    return datetime.now(timezone.utc) > deadline


def suspend_for_nonpayment(business: Business) -> None:
    """Suspend a business whose grace period has elapsed. Never deletes any data."""
    business.billing_status = "SUSPENDED"
    business.is_active = False
    business.suspended_reason = "Subscription payment overdue — grace period elapsed"
    logger.warning("Business %s suspended for non-payment (grace period elapsed)", business.id)
