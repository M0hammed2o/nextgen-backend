"""
BUG-0115 / BUG-0117 contract test — EVERY stocktake route must enforce the
Inventory entitlement.

Stocktake is a NextGen Operations (R1,499/month) feature. The original defect
was that `GET /stocktake` and `GET /stocktake/current` returned 200 for a
NextGen POS (R899) tenant while every sibling returned 409
INVENTORY_NOT_ENABLED — a commercial boundary that was unenforced on exactly
the handlers nothing asserted.

Fixing only the endpoints someone happened to notice is how this recurs: a
first pass fixed two, and an audit of the whole file then found two MORE with
the identical gap (`GET /{session_id}`, `GET /{session_id}/variance`). So this
test asserts the property over EVERY route in the module rather than over a
hand-written list of endpoints. A newly-added stocktake handler that forgets
the guard fails here immediately, which is the only thing that actually
prevents a third occurrence.
"""
import ast
import re
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[1] / "backend" / "app" / "api" / "v1" / "routes_stocktake.py"
GUARD = "_require_inventory_enabled"


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
            # @router.get(...) / @router.post(...)
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "router"):
                continue
            method = fn.attr.upper()
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                path = dec.args[0].value
            guarded = GUARD in ast.unparse(node)
            out.append((method, path, node.lineno, guarded))
    return out


def test_scan_found_the_handlers() -> None:
    """Guard against the AST scan silently finding nothing, which would make the
    contract test below vacuously pass."""
    handlers = _route_handlers()
    assert len(handlers) >= 8, f"expected to find the stocktake handlers, found {len(handlers)}"


def test_every_stocktake_route_enforces_the_inventory_entitlement() -> None:
    unguarded = [
        f"{m} /stocktake{p or ''} (line {ln})"
        for m, p, ln, guarded in _route_handlers()
        if not guarded
    ]
    assert unguarded == [], (
        "These stocktake routes do not call "
        f"{GUARD}(), so a NextGen POS (R899) tenant can reach an Operations "
        f"(R1,499) feature: {unguarded}. Stocktake is entitlement-gated — add "
        "the guard as the handler's first statement, as every sibling does."
    )
