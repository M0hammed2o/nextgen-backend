"""Pure tests for the offline-capture window (backend/app/events/trading.py)."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.core.errors import AppError
from backend.app.events.trading import (
    OFFLINE_CAPTURE_MAX_AGE,
    OFFLINE_CAPTURE_MAX_FUTURE_SKEW,
    validate_capture_window,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _code(captured):
    with pytest.raises(AppError) as exc:
        validate_capture_window(captured, NOW)
    return exc.value.status_code, exc.value.code


def test_constants():
    assert OFFLINE_CAPTURE_MAX_AGE == timedelta(hours=72)
    assert OFFLINE_CAPTURE_MAX_FUTURE_SKEW == timedelta(minutes=5)


def test_boundaries_are_inclusive_and_one_second_past_is_refused():
    assert validate_capture_window(NOW - OFFLINE_CAPTURE_MAX_AGE, NOW) == NOW - OFFLINE_CAPTURE_MAX_AGE
    assert _code(NOW - OFFLINE_CAPTURE_MAX_AGE - timedelta(seconds=1)) == (422, "OFFLINE_CAPTURE_TOO_OLD")
    assert validate_capture_window(NOW + OFFLINE_CAPTURE_MAX_FUTURE_SKEW, NOW)
    assert _code(NOW + OFFLINE_CAPTURE_MAX_FUTURE_SKEW + timedelta(seconds=1)) == (422, "OFFLINE_CAPTURE_IN_FUTURE")


def test_naive_is_refused_and_other_offsets_are_normalised_to_utc():
    assert _code(datetime(2026, 11, 19, 11, 0)) == (422, "OFFLINE_CAPTURE_NO_TIMEZONE")
    sast = timezone(timedelta(hours=2))
    out = validate_capture_window(datetime(2026, 11, 19, 13, 0, tzinfo=sast), NOW)
    assert out == datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc) and out.utcoffset() == timedelta(0)
