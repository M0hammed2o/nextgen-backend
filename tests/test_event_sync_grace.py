"""
BUG-0208 post-event sync grace: unit tests for the pure rule, the sync-only
allowlist and the get_current_user branch that consults it.

The DB-level behaviour (grace computed from real subscriptions, admin
suspension / cancellation / expiry refused, the review queue, concurrency) is
covered against real Postgres in tests/integration/test_souk_sync_grace_realdb.py.
"""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from backend.app.core import rbac
from backend.app.core.rbac import get_current_user
from backend.app.events import sync_grace
from backend.app.events.rules import PaidRange, last_paid_trade_day
from backend.app.events.service import EVENT_ADMIN_SUSPENSION_REASON, EVENT_SUSPENSION_REASON
from backend.app.events.trading import EVENT_SYNC_GRACE, validate_capture_window

GRACE_UNTIL = datetime(2026, 11, 22, 22, 0, tzinfo=timezone.utc)


# -- pure rule -----------------------------------------------------------------

def test_last_paid_trade_day_is_the_latest_billable_day_with_paid_cover():
    d = date(2026, 11, 20)
    billable = {d, d + timedelta(days=1), d + timedelta(days=2)}
    assert last_paid_trade_day(billable, []) is None
    assert last_paid_trade_day(billable, [PaidRange(d, d + timedelta(days=1))]) == d + timedelta(days=1)
    # a paid range over non-billable days grants no TRADE day
    assert last_paid_trade_day(billable, [PaidRange(d + timedelta(days=5), d + timedelta(days=9))]) is None
    assert last_paid_trade_day(set(), [PaidRange(d, d)]) is None
    # several ranges: the latest covered billable day wins
    assert last_paid_trade_day(billable, [PaidRange(d, d), PaidRange(d + timedelta(days=2), d + timedelta(days=2))]) \
        == d + timedelta(days=2)


def test_grace_constant_and_reason_mirror():
    assert EVENT_SYNC_GRACE == timedelta(hours=72)
    # rbac keeps its own copy to avoid importing the events package at load time
    assert rbac._EVENT_SUSPENSION_REASON == EVENT_SUSPENSION_REASON


def test_age_bound_can_only_be_skipped_explicitly():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=10)
    with pytest.raises(Exception) as exc:
        validate_capture_window(old, now)  # default bound still applies
    assert exc.value.code == "OFFLINE_CAPTURE_TOO_OLD"
    assert validate_capture_window(old, now, None) == old
    with pytest.raises(Exception) as exc:  # skipping the age bound never skips the future check
        validate_capture_window(now + timedelta(minutes=10), now, None)
    assert exc.value.code == "OFFLINE_CAPTURE_IN_FUTURE"
    with pytest.raises(Exception) as exc:
        validate_capture_window(datetime(2026, 11, 20, 12), now, None)
    assert exc.value.code == "OFFLINE_CAPTURE_NO_TIMEZONE"


# -- get_current_user ----------------------------------------------------------

class _Creds:
    credentials = "fake-token"


class _Result:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _DB:
    def __init__(self, row):
        self._row = row

    async def execute(self, stmt):
        return _Result(self._row)


def _payload(business_id, role="STAFF"):
    return {"sub": str(uuid.uuid4()), "bid": str(business_id), "role": role, "type": "access"}


def _route(path: str):
    route = MagicMock()
    route.path = path
    return route


class _Req:
    """Minimal stand-in for starlette's Request: method, matched route, cached JSON body."""

    def __init__(self, method: str, path: str | None, body=None, raw: bytes | None = None):
        self.method = method
        self.scope = {"route": _route(path)} if path is not None else {}
        self._body = body
        self._raw = raw

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        if self._body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return self._body


async def _call(request, row, grace=GRACE_UNTIL, reason_check=None):
    business_id = uuid.uuid4()
    grace_mock = AsyncMock(return_value=grace)
    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)), \
         patch("backend.app.events.trading.event_sync_grace_until", grace_mock):
        user = await get_current_user(request=request, credentials=_Creds(), db=_DB(row))
    return user, grace_mock


async def _refused(request, row, grace=GRACE_UNTIL):
    with pytest.raises(HTTPException) as exc:
        await _call(request, row, grace)
    return exc.value.status_code, exc.value.detail["code"]


ENDED = (True, False, EVENT_SUSPENSION_REASON, 0)  # user_active, business_active, reason, token_version


async def test_grace_allows_an_allowlisted_route_and_marks_the_user_sync_only():
    user, grace = await _call(_Req("GET", "/v1/business/event-access"), ENDED)
    assert user.sync_only_until == GRACE_UNTIL
    grace.assert_awaited_once()


async def test_grace_refuses_everything_off_the_allowlist():
    for method, path in [
        ("POST", "/v1/business/menu/items"),
        ("PUT", "/v1/business/menu/items/{item_id}"),
        ("POST", "/v1/business/staff"),
        ("POST", "/v1/business/pos/orders/{order_id}/refund"),
        ("POST", "/v1/business/pos/orders/{order_id}/void"),
        ("PATCH", "/v1/business/orders/{order_id}/payment"),
        ("POST", "/v1/business/pos/till/open"),
        ("POST", "/v1/business/pos/payment-intents"),
        ("POST", "/v1/business/event-subscriptions"),
        ("GET", "/v1/business/orders/live/stream"),
        # right path, wrong method
        ("DELETE", "/v1/business/event-access"),
        ("POST", "/v1/business/orders"),
    ]:
        assert await _refused(_Req(method, path), ENDED) == (403, "EVENT_SYNC_ONLY"), (method, path)


async def test_grace_refuses_when_no_route_was_matched():
    assert await _refused(_Req("GET", None), ENDED) == (403, "EVENT_SYNC_ONLY")


async def test_grace_pos_orders_needs_an_offline_capture_in_the_body():
    online = {"items": [{"menu_item_id": str(uuid.uuid4()), "quantity": 1}], "payment_method": "CASH",
              "cash_tendered_cents": 100}
    assert await _refused(_Req("POST", "/v1/business/pos/orders", online), ENDED) == (403, "EVENT_SYNC_ONLY")
    assert await _refused(_Req("POST", "/v1/business/pos/orders", {**online, "offline_captured_at": None}),
                          ENDED) == (403, "EVENT_SYNC_ONLY")
    assert await _refused(_Req("POST", "/v1/business/pos/orders", None), ENDED) == (403, "EVENT_SYNC_ONLY")
    assert await _refused(_Req("POST", "/v1/business/pos/orders", ["not", "a", "dict"]), ENDED) == (
        403, "EVENT_SYNC_ONLY")
    offline = {**online, "offline_captured_at": "2026-11-20T10:00:00+02:00", "idempotency_key": "k"}
    user, _ = await _call(_Req("POST", "/v1/business/pos/orders", offline), ENDED)
    assert user.sync_only_until == GRACE_UNTIL


async def test_no_grace_window_means_the_old_suspension():
    assert await _refused(_Req("GET", "/v1/business/event-access"), ENDED, grace=None) == (
        403, "BUSINESS_SUSPENDED")


@pytest.mark.parametrize("reason", [EVENT_ADMIN_SUSPENSION_REASON, "Payment overdue", None, "TRIAL_EXPIRED",
                                    "event_subscription_ended"])
async def test_any_other_suspension_reason_never_reaches_the_grace_check(reason):
    row = (True, False, reason, 0)
    with pytest.raises(HTTPException) as exc:
        user, grace = await _call(_Req("GET", "/v1/business/event-access"), row)
    assert exc.value.detail["code"] == "BUSINESS_SUSPENDED"
    # and the grace computation is not even consulted
    business_id = uuid.uuid4()
    grace_mock = AsyncMock(return_value=GRACE_UNTIL)
    with patch("backend.app.core.rbac.decode_access_token", return_value=_payload(business_id)), \
         patch("backend.app.events.trading.event_sync_grace_until", grace_mock):
        with pytest.raises(HTTPException):
            await get_current_user(request=_Req("GET", "/v1/business/event-access"), credentials=_Creds(),
                                   db=_DB(row))
    grace_mock.assert_not_awaited()


async def test_an_active_business_is_never_marked_sync_only():
    user, grace = await _call(_Req("POST", "/v1/business/menu/items"), (True, True, None, 0))
    assert user.sync_only_until is None
    grace.assert_not_awaited()


async def test_a_deactivated_user_or_revoked_session_is_refused_before_grace():
    assert await _refused(_Req("GET", "/v1/business/event-access"), (False, False, EVENT_SUSPENSION_REASON, 0)) == (
        403, "ACCOUNT_DISABLED")
    assert await _refused(_Req("GET", "/v1/business/event-access"), (True, False, EVENT_SUSPENSION_REASON, 7)) == (
        401, "SESSION_REVOKED")


# -- the allowlist against the real application -------------------------------

def _app_routes():
    from backend.app.main import app
    return [(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods]


def test_every_allowlisted_route_exists():
    real = set(_app_routes())
    missing = sync_grace.sync_only_allowlist() - real
    assert not missing, f"allowlist names routes that do not exist: {missing}"


def test_the_allowlist_is_exactly_the_approved_set():
    assert sync_grace.sync_only_allowlist() == {
        ("GET", "/v1/auth/me"),
        ("GET", "/v1/business/event-access"),
        ("POST", "/v1/business/pos/orders"),
        ("GET", "/v1/business/pos/offline-orders/flagged"),
        ("POST", "/v1/business/pos/offline-orders/{order_id}/acknowledge"),
        ("GET", "/v1/business/pos/offline-reviews"),
        ("POST", "/v1/business/pos/offline-reviews/{review_id}/approve"),
        ("POST", "/v1/business/pos/offline-reviews/{review_id}/reject"),
        ("GET", "/v1/business/pos/till/current"),
        ("POST", "/v1/business/pos/till/{session_id}/close"),
        ("GET", "/v1/business/orders"),
        ("GET", "/v1/business/orders/{order_id}"),
    }


async def test_every_other_business_route_in_the_app_is_refused_during_grace():
    allowed = sync_grace.sync_only_allowlist()
    checked = 0
    for method, path in _app_routes():
        if (method, path) in allowed:
            continue
        with pytest.raises(HTTPException) as exc:
            await sync_grace.assert_sync_only_request_allowed(_Req(method, path, {"offline_captured_at": "x"}))
        assert exc.value.detail["code"] == "EVENT_SYNC_ONLY", (method, path)
        checked += 1
    assert checked > 100  # the whole application, not a handful


# -- request schema ------------------------------------------------------------

def test_review_opt_in_is_only_valid_on_an_offline_capture():
    from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest
    base = dict(items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)], payment_method="CASH",
                cash_tendered_cents=100)
    with pytest.raises(ValidationError):
        POSOrderCreateRequest(**base, offline_review_if_too_old=True)
    assert POSOrderCreateRequest(**base).offline_review_if_too_old is False
    ok = POSOrderCreateRequest(**base, offline_captured_at=datetime.now(timezone.utc), idempotency_key="k",
                               offline_review_if_too_old=True)
    assert ok.offline_review_if_too_old is True


def test_review_decision_note_is_required():
    from backend.app.api.v1.routes_pos import OfflineReviewDecisionRequest
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            OfflineReviewDecisionRequest(note=bad)
    with pytest.raises(ValidationError):
        OfflineReviewDecisionRequest()
    assert OfflineReviewDecisionRequest(note="  counted cash matches  ").note == "counted cash matches"


async def test_create_pos_order_refuses_an_online_sale_for_a_sync_only_user():
    from backend.app.api.v1.routes_pos import POSOrderCreateRequest, POSOrderItemRequest, create_pos_order
    from backend.app.core.errors import AppError
    from backend.app.core.rbac import AuthUser
    user = AuthUser(user_id=uuid.uuid4(), business_id=uuid.uuid4(), role="OWNER", token_type="access",
                    sync_only_until=GRACE_UNTIL)
    body = POSOrderCreateRequest(items=[POSOrderItemRequest(menu_item_id=uuid.uuid4(), quantity=1)],
                                 payment_method="CASH", cash_tendered_cents=100)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("must refuse before touching the database"))
    with pytest.raises(AppError) as exc:
        await create_pos_order(body, user, db)
    assert (exc.value.status_code, exc.value.code) == (403, "EVENT_SYNC_ONLY")
