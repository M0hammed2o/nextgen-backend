"""
F5 fix (Wave 0A verifier) — HTTP-level regression, ported from the
independent verifier's own adversarial reproduction (R13 / superadmin sweep).
Confirms every business-plane route that authenticates via get_current_user
rejects an admin-plane (SUPER_ADMIN/ONBOARDER) token with 401/403 — never a
200 (data leak) or 500 (unhandled crash, e.g. the pre-fix GET /loyalty/program).

In-process ASGI via httpx.ASGITransport — does NOT run the app's lifespan
(no Redis/ARQ pool needed), and every route in this sweep must be rejected
by the auth/RBAC dependency layer before any DB call is made, so this needs
no real database either. A network guard blocks any accidental outbound
httpx call during the sweep (defense in depth — nothing in this path should
ever reach out, but a payment/WhatsApp route wired incorrectly might).
"""
import re
import uuid
from collections import Counter

import httpx
import pytest


# GET /v1/auth/me is deliberately excluded: it depends on bare
# get_current_user (the ONLY route in backend/app/api/v1 that does — every
# other route goes through the require_business_access family, which is
# exactly what the F5 fix changed). Its behaviour for an admin-plane token
# is unrelated to and unaffected by this fix either way — it was never
# gated by business_id, and it 404s (NotFoundError) against a real DB
# because no BusinessUser row matches an admin's id, or fails with a
# connection error in a DB-less test run — neither of which this sweep's
# {401, 403}-only check can assert meaningfully without a live database.
_EXCLUDED_ROUTES = {("GET", "/v1/auth/me")}


def _all_business_routes():
    from fastapi.routing import APIRoute

    from backend.app.core.rbac import get_current_user
    from backend.app.main import app

    def uses_auth(dep) -> bool:
        if dep.call is get_current_user:
            return True
        return any(uses_auth(d) for d in dep.dependencies)

    out = []
    for route in app.routes:
        if isinstance(route, APIRoute) and uses_auth(route.dependant):
            for method in route.methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                if (method, route.path) in _EXCLUDED_ROUTES:
                    continue
                out.append((method, route.path))
    return app, out


@pytest.mark.parametrize("role", ["ONBOARDER", "SUPER_ADMIN"])
async def test_admin_token_sweep_rejected_on_every_business_route(role, monkeypatch):
    real_send = httpx.AsyncClient.send

    async def guarded_send(self, request, *a, **k):
        if request.url.host in ("test", "testserver"):
            return await real_send(self, request, *a, **k)
        raise RuntimeError(f"unexpected outbound network call blocked: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send)

    from backend.app.core.security import create_admin_access_token

    app, routes = _all_business_routes()
    assert len(routes) > 20, "sanity check: the business-plane route sweep found suspiciously few routes"

    token = create_admin_access_token(uuid.uuid4(), role=role)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    results: dict[tuple[str, str], object] = {}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in routes:
            concrete_path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
            try:
                resp = await client.request(
                    method, concrete_path, headers={"Authorization": f"Bearer {token}"}, json={},
                )
                results[(method, path)] = resp.status_code
            except Exception as exc:  # noqa: BLE001 — captured for the report below
                results[(method, path)] = f"EXCEPTION {type(exc).__name__}: {exc}"

    bad = {k: v for k, v in results.items() if not (isinstance(v, int) and v in (401, 403))}
    if bad:
        summary = "\n".join(f"  {v!r:>6}  {m} {p}" for (m, p), v in sorted(bad.items(), key=str))
        pytest.fail(
            f"{role} token got a non-401/403 response on {len(bad)} business-plane "
            f"route(s) (of {len(routes)} checked):\n{summary}"
        )

    print(role, "routes checked:", len(routes), "status distribution:", Counter(str(v) for v in results.values()))


@pytest.mark.parametrize("path", [
    "/v1/business/loyalty/program",
    "/v1/business/orders",
    "/v1/business/menu/categories",
    "/v1/business/menu/items",
    "/v1/business/settings",
])
@pytest.mark.parametrize("role", ["ONBOARDER", "SUPER_ADMIN"])
async def test_admin_token_representative_routes_explicit_403(path, role):
    """
    The specific routes named in the task brief, asserted individually (not
    just swept) so a future regression here fails loudly and specifically —
    GET /loyalty/program in particular used to return an unhandled 500 for
    a SUPER_ADMIN token before this fix.
    """
    from backend.app.core.security import create_admin_access_token
    from backend.app.main import app

    token = create_admin_access_token(uuid.uuid4(), role=role)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, f"{role} on {path} -> {resp.status_code}: {resp.text[:300]}"
    assert resp.json()["detail"]["code"] == "NO_BUSINESS_CONTEXT"
