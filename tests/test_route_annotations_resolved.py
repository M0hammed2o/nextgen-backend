"""
Every route parameter must resolve to a real type. If FastAPI is left with
an unresolved ForwardRef it silently treats the parameter as a required
query parameter, so the request body can never be sent.

That happened to POST /v1/public/events/{slug}/register: the module used
``from __future__ import annotations`` and the route is wrapped by slowapi's
``shared_limit``, so FastAPI resolved "RegisterRequest" against slowapi's
globals. Every registration got 422 {"loc": ["query", "body"]}. The HTTP
end-to-end run found it; calling the route function directly never could.
"""
from typing import ForwardRef

from fastapi.routing import APIRoute


def _unresolved(app) -> list[str]:
    bad = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        params = (route.dependant.query_params + route.dependant.body_params
                  + route.dependant.path_params + route.dependant.header_params)
        for p in params:
            ann = getattr(p.field_info, "annotation", None)
            if isinstance(ann, ForwardRef) or isinstance(ann, str):
                bad.append(f"{sorted(route.methods)} {route.path} -> {p.name}: {ann!r}")
    return bad


def test_business_api_has_no_unresolved_route_annotations():
    from backend.app.main import app

    assert _unresolved(app) == []


def test_admin_api_has_no_unresolved_route_annotations():
    from admin_api.app.main import app

    assert _unresolved(app) == []


def test_public_event_registration_takes_a_json_body():
    from backend.app.main import app

    route = next(r for r in app.routes if isinstance(r, APIRoute)
                 and r.path == "/v1/public/events/{slug}/register")
    assert [q.name for q in route.dependant.query_params] == []
    assert route.body_field is not None


def test_public_event_registration_over_http_reaches_validation_of_the_body():
    """Over real HTTP an empty JSON object must fail on the body's own fields,
    not on a phantom ``query.body`` parameter."""
    from fastapi.testclient import TestClient

    from backend.app.main import app

    # No ``with``: lifespan startup (Redis/ARQ) is not needed, request validation
    # runs before the handler and before any database access.
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/v1/public/events/some-slug/register", json={})
    assert resp.status_code == 422
    locs = [tuple(e["loc"]) for e in resp.json()["error"]["details"]["errors"]]
    assert ("query", "body") not in locs
    assert any(loc[0] == "body" for loc in locs)
