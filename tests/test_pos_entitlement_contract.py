"""
migration 0057_pos_entitlement — contract test: EVERY route in
routes_pos.py must enforce `require_pos_enabled` (backend/app/core/rbac.py)
via `dependencies=[Depends(require_pos_enabled)]`, EXCEPT
`GET /business/pos/settings` (deliberately excluded — the Staff PWA reads
it as general tenant config, not just POS config).

Same AST-scan technique as tests/test_stocktake_entitlement_contract.py —
a hand-written list of "the routes I remembered to check" is exactly how a
future new POS route forgetting the guard would slip through unnoticed.
This test asserts the property over EVERY route in the module instead.

Also unit-tests `require_pos_enabled` itself in isolation (fail-closed on
False/missing business, passes through on True, no business_id -> 403),
using the same lightweight FakeDB/FakeResult style already established in
tests/test_pos_refund_void.py — the real-DB behavioural proof (reading an
actual `Business.pos_enabled` column) lives in
tests/integration/test_pos_entitlement_and_whatsapp_usage.py.
"""
import ast
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.core.rbac import AuthUser, require_pos_enabled

ROUTES = Path(__file__).resolve().parents[1] / "backend" / "app" / "api" / "v1" / "routes_pos.py"
GUARD = "require_pos_enabled"
EXCLUDED_PATH = "/settings"


def _route_handlers() -> list[tuple[str, str, int, bool]]:
    """(http_method, path, lineno, guarded) for every @router-decorated handler."""
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"), filename=str(ROUTES))
    out: list[tuple[str, str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "router"):
                continue
            method = fn.attr.upper()
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                path = dec.args[0].value
            guarded = GUARD in ast.unparse(dec)
            out.append((method, path, node.lineno, guarded))
    return out


def test_scan_found_the_handlers() -> None:
    """Guard against the AST scan silently finding nothing, which would make
    the contract test below vacuously pass."""
    handlers = _route_handlers()
    assert len(handlers) >= 14, f"expected to find the POS handlers, found {len(handlers)}"


def test_every_pos_route_except_settings_enforces_pos_enabled() -> None:
    unguarded = [
        f"{m} {p} (line {ln})"
        for m, p, ln, guarded in _route_handlers()
        if not guarded and p != EXCLUDED_PATH
    ]
    assert unguarded == [], (
        "These POS routes do not declare dependencies=[Depends(require_pos_enabled)], "
        f"so a pos_enabled=False (WhatsApp-only) tenant could reach them: {unguarded}. "
        "Add the dependency to the route decorator, as every sibling does."
    )


def test_settings_route_deliberately_not_gated() -> None:
    """The one deliberate exception — asserted explicitly so a future
    session doesn't 'fix' this by gating it, breaking the Staff PWA's
    general-config read for a WhatsApp-only tenant."""
    settings_routes = [(m, p, ln, guarded) for m, p, ln, guarded in _route_handlers() if p == EXCLUDED_PATH]
    assert len(settings_routes) == 1, f"expected exactly one {EXCLUDED_PATH} route, found {settings_routes}"
    assert settings_routes[0][3] is False


# ── require_pos_enabled unit behaviour (FakeDB style) ───────────────────────

class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, value):
        self._value = value

    async def execute(self, stmt):
        return _FakeScalarResult(self._value)


def _make_user(business_id=None, role="STAFF"):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role=role, token_type="access")


async def test_require_pos_enabled_passes_when_true():
    user = _make_user(business_id=uuid.uuid4())
    db = _FakeDB(True)
    await require_pos_enabled(user=user, db=db)  # must not raise


async def test_require_pos_enabled_raises_409_when_false():
    user = _make_user(business_id=uuid.uuid4())
    db = _FakeDB(False)
    with pytest.raises(HTTPException) as exc:
        await require_pos_enabled(user=user, db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "POS_NOT_ENABLED", "message": "POS is not enabled for this business"}


async def test_require_pos_enabled_fails_closed_when_business_missing():
    """A business row that can't be found (scalar_one_or_none() -> None)
    must be treated exactly like POS being off, never 'allowed by default'."""
    user = _make_user(business_id=uuid.uuid4())
    db = _FakeDB(None)
    with pytest.raises(HTTPException) as exc:
        await require_pos_enabled(user=user, db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "POS_NOT_ENABLED"


async def test_require_pos_enabled_rejects_missing_business_context():
    user = _make_user(business_id=None, role="SUPER_ADMIN")
    db = _FakeDB(True)
    with pytest.raises(HTTPException) as exc:
        await require_pos_enabled(user=user, db=db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"
