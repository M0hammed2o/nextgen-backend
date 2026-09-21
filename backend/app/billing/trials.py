"""
Trial lifecycle — start/extend a commercial trial (Wave 0A sales-onboarding).

Deliberately does NOT introduce a second "trial status" column. The stored
facts remain exactly what already existed: `Business.billing_status`
("TRIAL" while trialling — unchanged the whole time a trial is simply
running past its end date) and `Business.trial_ends_at`. Whether a trial
reads as live/expiring-soon/expired is always DERIVED at read time by
`backend/app/billing/account_status.py::compute_account_status` — never
stored twice, so the two views can never disagree.
"""

from datetime import datetime, timedelta, timezone

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from shared.models.business import Business
from shared.utils.time import end_of_local_day_utc, to_business_tz


def compute_trial_end(tz_name: str, days: int, *, from_now: datetime | None = None) -> datetime:
    """
    trial_ends_at = the END of the local calendar day `days` full days after
    `from_now`'s own local calendar date — e.g. a 14-day trial started at
    any time of day today ends at 23:59:59.999999 local on (today + 14).
    """
    now = from_now or datetime.now(timezone.utc)
    start_local_date = to_business_tz(now, tz_name).date()
    end_local_date = start_local_date + timedelta(days=days)
    return end_of_local_day_utc(end_local_date, tz_name)


def _unsuspend_if_trial_expired(business: Business) -> None:
    """
    Only ever lifts a suspension this same trial-lifecycle code applied
    (`suspended_reason == "TRIAL_EXPIRED"`, set by the hourly cron — see
    backend/app/worker.py::suspend_past_due_businesses). Never touches a
    suspension for any other reason (e.g. a manual SUPER_ADMIN suspend, or
    non-payment) — those must be lifted explicitly via their own route.
    """
    if not business.is_active and business.suspended_reason == "TRIAL_EXPIRED":
        business.is_active = True
        business.suspended_reason = None


def start_trial(business: Business, *, now: datetime | None = None) -> None:
    """(Re)start a trial for `business`: 14 days (config) from now, extension
    counter reset. Used both at business-creation time (start_trial=true,
    the default) and by POST /admin/businesses/{id}/trial/start for an
    existing business with no trial running."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    tz = business.timezone or "Africa/Johannesburg"

    business.billing_status = "TRIAL"
    business.trial_started_at = now
    business.trial_extension_days_total = 0
    business.trial_ends_at = compute_trial_end(tz, settings.TRIAL_DEFAULT_DAYS, from_now=now)
    _unsuspend_if_trial_expired(business)


def extend_trial(
    business: Business, days: int, *, is_onboarder: bool, now: datetime | None = None,
) -> None:
    """
    Push `business.trial_ends_at` `days` further out (from whatever its
    current value is — including a value already in the past, i.e. an
    expired trial, which this "reactivates" simply by moving the derived
    status back to TRIAL). Raises AppError(403) if an ONBOARDER's
    cumulative extensions on this business would exceed
    ONBOARDER_MAX_TRIAL_EXTENSION_DAYS; SUPER_ADMIN (is_onboarder=False) is
    unlimited. Caller is responsible for requiring/validating `reason` —
    this function only performs the state change.
    """
    if days <= 0:
        raise AppError("INVALID_EXTENSION", "days must be a positive integer", 422)

    settings = get_settings()
    if is_onboarder:
        cap = settings.ONBOARDER_MAX_TRIAL_EXTENSION_DAYS
        if business.trial_extension_days_total + days > cap:
            raise AppError(
                "EXTENSION_CAP_EXCEEDED",
                f"ONBOARDER trial extensions are capped at {cap} cumulative days per "
                f"business (already used {business.trial_extension_days_total})",
                403,
            )

    tz = business.timezone or "Africa/Johannesburg"
    resolved_now = now or datetime.now(timezone.utc)
    current_end_date = (
        to_business_tz(business.trial_ends_at, tz).date()
        if business.trial_ends_at is not None
        else to_business_tz(resolved_now, tz).date()
    )
    new_end_date = current_end_date + timedelta(days=days)

    business.billing_status = "TRIAL"
    business.trial_ends_at = end_of_local_day_utc(new_end_date, tz)
    business.trial_extension_days_total += days
    _unsuspend_if_trial_expired(business)
