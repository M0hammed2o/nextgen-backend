"""
BUG-0208: the sync-only allowlist for an event vendor inside its post-event
sync grace.

This is a deliberately narrow RELAXATION of a suspension, so it is kept small
and explicit. ``get_current_user`` (backend/app/core/rbac.py) only consults it
after ``event_sync_grace_until`` (backend/app/events/trading.py) has confirmed,
live from the database, that the business is an EVENT-plan business suspended
with exactly EVENT_SUBSCRIPTION_ENDED and still inside EVENT_SYNC_GRACE of its
last paid day. An administrator's suspension, a cancellation, any other
suspension reason and every non-event business never reach this module.

Routes are matched on the HTTP method and the ROUTE TEMPLATE FastAPI matched
(``scope["route"].path``, e.g. ``/v1/business/pos/till/{session_id}/close``),
never on a client-controlled string, so a crafted URL cannot alias an
allowlisted path. Anything not listed is refused with 403 EVENT_SYNC_ONLY.

What is allowed, and why:
- GET  /auth/me                         stay logged in (login, PIN login,
                                        refresh and logout do not use this
                                        dependency; auth_service allows them
                                        during grace too)
- GET  /business/event-access           the app learns it is sync-only
- POST /business/pos/orders             ONLY with offline_captured_at in the
                                        body (an upload of a queued sale). An
                                        online sale is refused here and again
                                        in create_pos_order.
- GET  /business/pos/offline-orders/flagged, POST .../{id}/acknowledge
                                        reconciliation status / owner review
- GET  /business/pos/offline-reviews, POST .../{id}/approve, .../{id}/reject
                                        the late-upload review queue
- GET  /business/pos/till/current, POST /business/pos/till/{id}/close
                                        cash-up of the session the offline
                                        sales belong to. close_till records the
                                        counted cash and computes expected cash
                                        from sales already recorded; it creates
                                        no sale and no money movement. Opening
                                        a till is NOT allowed.
- GET  /business/orders, GET /business/orders/{id}
                                        read-only order history
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from backend.app.core.config import get_settings

# (method, path relative to API_V1_PREFIX). Keep this list short.
_RELATIVE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/auth/me"),
    ("GET", "/business/event-access"),
    ("POST", "/business/pos/orders"),
    ("GET", "/business/pos/offline-orders/flagged"),
    ("POST", "/business/pos/offline-orders/{order_id}/acknowledge"),
    ("GET", "/business/pos/offline-reviews"),
    ("POST", "/business/pos/offline-reviews/{review_id}/approve"),
    ("POST", "/business/pos/offline-reviews/{review_id}/reject"),
    ("GET", "/business/pos/till/current"),
    ("POST", "/business/pos/till/{session_id}/close"),
    ("GET", "/business/orders"),
    ("GET", "/business/orders/{order_id}"),
})
# The one allowlisted route that is conditional on its body.
_OFFLINE_ONLY = ("POST", "/business/pos/orders")


def sync_only_allowlist() -> frozenset[tuple[str, str]]:
    prefix = get_settings().API_V1_PREFIX
    return frozenset((m, prefix + p) for m, p in _RELATIVE_ALLOWLIST)


def sync_only_refusal(message: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "EVENT_SYNC_ONLY",
            "message": message or (
                "This event has ended. Only uploading outstanding offline sales and "
                "reviewing reconciliation are available."
            ),
        },
    )


async def assert_sync_only_request_allowed(request: Request) -> None:
    """Raise 403 EVENT_SYNC_ONLY unless this request is on the sync-only allowlist."""
    route = request.scope.get("route") if hasattr(request, "scope") else None
    template = getattr(route, "path", None)
    method = (request.method or "").upper() if isinstance(getattr(request, "method", None), str) else ""
    if not isinstance(template, str) or (method, template) not in sync_only_allowlist():
        raise sync_only_refusal()
    prefix = get_settings().API_V1_PREFIX
    if (method, template) == (_OFFLINE_ONLY[0], prefix + _OFFLINE_ONLY[1]):
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict) or not body.get("offline_captured_at"):
            raise sync_only_refusal(
                "This event has ended. New sales are not allowed; only queued offline sales can be uploaded."
            )
