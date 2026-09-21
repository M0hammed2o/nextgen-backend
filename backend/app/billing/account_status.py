"""
The ONE function that derives a business's displayed commercial status.

Stored columns (`Business.is_active`, `.billing_status`, `.trial_ends_at`,
`CommercialInvoice.status`/`.due_date`) are the lifecycle FACTS. Everything
shown to an owner, an ONBOARDER, or a SUPER_ADMIN about "where does this
account stand right now" must go through `compute_account_status()` so the
displayed status can never drift from those facts — see this task's design
note and DECISIONS.md DOC NOTE.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from shared.models.business import Business
from shared.utils.time import to_business_tz

STATUS_TRIAL = "TRIAL"
STATUS_ACTIVE = "ACTIVE"
STATUS_PAYMENT_DUE = "PAYMENT_DUE"
STATUS_OVERDUE = "OVERDUE"
STATUS_EXPIRED = "EXPIRED"
STATUS_SUSPENDED = "SUSPENDED"
STATUS_CANCELLED = "CANCELLED"

ALERT_TRIAL_ENDS_7D = "TRIAL_ENDS_7D"
ALERT_TRIAL_ENDS_3D = "TRIAL_ENDS_3D"
ALERT_TRIAL_ENDS_1D = "TRIAL_ENDS_1D"
ALERT_TRIAL_EXPIRED = "TRIAL_EXPIRED"
ALERT_INVOICE_OVERDUE = "INVOICE_OVERDUE"
ALERT_PAYMENT_AWAITING = "PAYMENT_AWAITING"
ALERT_SUSPENDED = "SUSPENDED"


@dataclass
class AccountStatus:
    status: str
    trial_days_remaining: int | None
    alerts: list[str] = field(default_factory=list)


def _invoice_due_date(invoice) -> date | None:
    """Accepts a CommercialInvoice ORM row, a plain object, or a dict —
    tests build these three different ways (see TESTS section of the task
    brief); all this needs is a `due_date`."""
    if isinstance(invoice, dict):
        return invoice.get("due_date")
    return getattr(invoice, "due_date", None)


def compute_account_status(
    business: Business,
    open_invoices: list,
    now: datetime | None = None,
) -> AccountStatus:
    """
    `open_invoices` — every ISSUED, UNPAID `CommercialInvoice` for this
    business (status in SENT/AWAITING_PAYMENT/OVERDUE — see
    shared.models.commercial_invoice.UNPAID_ISSUED_STATUSES). The caller
    queries these; this function only reasons about their `due_date`s, so
    the OVERDUE/PAYMENT_DUE distinction is always computed live rather than
    trusting a possibly-stale stored `CommercialInvoice.status`.

    Precedence (first match wins):
      1. business.is_active is False           -> SUSPENDED
      2. business.billing_status == CANCELLED   -> CANCELLED
      3. business.billing_status == TRIAL       -> TRIAL (now < trial_ends_at)
                                                     or EXPIRED
      4. any open invoice past due, OR
         billing_status == PAST_DUE             -> OVERDUE
      5. any open invoice not yet due           -> PAYMENT_DUE
      6. otherwise                              -> ACTIVE

    Day maths (trial remaining / due-vs-not-yet-due) is always done on LOCAL
    CALENDAR DATES in the business's own timezone, never a raw UTC/24h delta
    — a business at 23:50 local a few minutes into its final trial day must
    not read as "2 days left" just because slightly less than 48 raw hours
    remain.
    """
    now = now or datetime.now(timezone.utc)
    tz = business.timezone or "Africa/Johannesburg"

    if not business.is_active:
        return AccountStatus(status=STATUS_SUSPENDED, trial_days_remaining=None, alerts=[ALERT_SUSPENDED])

    if business.billing_status == "CANCELLED":
        return AccountStatus(status=STATUS_CANCELLED, trial_days_remaining=None, alerts=[])

    if business.billing_status == "TRIAL":
        return _trial_status(business, now, tz)

    # Derived from the SAME `now` passed in (never a fresh wall-clock read —
    # shared.utils.time.today_date_for_business() always reads the real
    # current time internally, which would silently ignore a caller-supplied
    # `now` and make this function untestable/inconsistent at exact day
    # boundaries; found and fixed while writing this module's own tests).
    today_local = to_business_tz(now, tz).date()
    due_dates = [d for d in (_invoice_due_date(inv) for inv in open_invoices) if d is not None]
    is_overdue = business.billing_status == "PAST_DUE" or any(d < today_local for d in due_dates)
    if is_overdue:
        return AccountStatus(status=STATUS_OVERDUE, trial_days_remaining=None, alerts=[ALERT_INVOICE_OVERDUE])
    if due_dates:
        return AccountStatus(status=STATUS_PAYMENT_DUE, trial_days_remaining=None, alerts=[ALERT_PAYMENT_AWAITING])
    return AccountStatus(status=STATUS_ACTIVE, trial_days_remaining=None, alerts=[])


def _trial_status(business: Business, now: datetime, tz: str) -> AccountStatus:
    trial_ends_at = business.trial_ends_at
    if trial_ends_at is None or now >= trial_ends_at:
        return AccountStatus(status=STATUS_EXPIRED, trial_days_remaining=0, alerts=[ALERT_TRIAL_EXPIRED])

    today_local = to_business_tz(now, tz).date()
    end_local_date = to_business_tz(trial_ends_at, tz).date()
    days_remaining = max(0, (end_local_date - today_local).days)

    alerts: list[str] = []
    if days_remaining <= 1:
        alerts.append(ALERT_TRIAL_ENDS_1D)
    elif days_remaining <= 3:
        alerts.append(ALERT_TRIAL_ENDS_3D)
    elif days_remaining <= 7:
        alerts.append(ALERT_TRIAL_ENDS_7D)

    return AccountStatus(status=STATUS_TRIAL, trial_days_remaining=days_remaining, alerts=alerts)
