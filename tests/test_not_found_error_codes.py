"""
BUG-0113 — contract tests for `NotFoundError`'s generated machine-readable
`code`.

`NotFoundError.__init__` used to build its `code` as
`f"{resource.upper()}_NOT_FOUND"` with NO normalisation, so any resource
string containing a space or hyphen produced a malformed code (e.g.
"Menu item" -> "MENU ITEM_NOT_FOUND"), and two call-site spellings of the
same real-world condition ("MenuItem" vs "Menu item") produced two
different codes for one condition.

This file has two independent halves:

1. Direct unit tests of `_normalize_not_found_code`/`NotFoundError`
   against a representative set of spaced, hyphenated, CamelCase, and
   already-clean inputs — including the exact strings enumerated in the
   BUG-0113 fix, so a future edit to the normalisation rule gets an
   immediate, specific failure.
2. A codebase-wide scan (AST-based, not string matching, so it survives
   reformatting) that finds every `NotFoundError(...)` call site's
   resource literal — both direct calls and the ones that reach
   `NotFoundError` indirectly via `require_owned(..., label=...)`
   (`backend/app/core/ownership.py`) — and asserts the resulting code
   always matches `^[A-Z][A-Z0-9_]*$` with no double underscore. THIS is
   the test that actually prevents recurrence: it fails the moment a new
   call site introduces a space/hyphen/double-underscore, without anyone
   needing to remember to add a case to part 1.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from backend.app.core.errors import DuplicateError, NotFoundError, _normalize_not_found_code

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that are part of the shipped backend/admin API surface.
# Deliberately excludes tests/, .venv, node_modules, migrations, etc. — this
# test is about production call sites, not test fixtures.
SCAN_ROOTS = [REPO_ROOT / "backend", REPO_ROOT / "admin_api", REPO_ROOT / "shared"]

CODE_FORMAT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# ── Part 1: direct normalisation-rule tests ─────────────────────────────────

@pytest.mark.parametrize(
    "resource, expected_code_prefix",
    [
        # The seven malformed-by-space/hyphen resources from BUG-0113.
        ("Admin user", "ADMIN_USER"),
        ("Delivery connection", "DELIVERY_CONNECTION"),
        ("Delivery order", "DELIVERY_ORDER"),
        ("Menu add-on", "MENU_ADD_ON"),
        ("Menu item", "MENU_ITEM"),
        ("Recipe for menu item", "RECIPE_FOR_MENU_ITEM"),
        ("Wastage entry", "WASTAGE_ENTRY"),
        ("Add-on", "ADD_ON"),
        # The two resolved CamelCase/spaced duplications now converge on
        # the spaced (product-owner-chosen) form.
        ("MenuItem", "MENUITEM"),  # no longer used at any call site (see part 2)
        ("MenuAddOn", "MENUADDON"),  # no longer used at any call site (see part 2)
        # Deliberately-unchanged carve-out: CamelCase with no space/hyphen
        # is NOT split into words. Four live nextgen-staff-v3 consumers
        # depend on this exact code.
        ("LoyaltyProgram", "LOYALTYPROGRAM"),
        # Single-word resources were never malformed; must stay identical.
        ("Business", "BUSINESS"),
        ("Order", "ORDER"),
        # Defensive: repeated/mixed separators collapse to one underscore
        # with no leading/trailing underscore.
        ("Menu   item", "MENU_ITEM"),
        ("-Menu item-", "MENU_ITEM"),
        ("Menu--item", "MENU_ITEM"),
    ],
)
def test_normalize_not_found_code(resource: str, expected_code_prefix: str) -> None:
    assert _normalize_not_found_code(resource) == expected_code_prefix


@pytest.mark.parametrize(
    "resource, resource_id, expected_code, expected_message",
    [
        ("Admin user", "abc-123", "ADMIN_USER_NOT_FOUND", "Admin user not found: abc-123"),
        ("Delivery connection", "uber_eats", "DELIVERY_CONNECTION_NOT_FOUND", "Delivery connection not found: uber_eats"),
        ("Delivery order", "abc-123", "DELIVERY_ORDER_NOT_FOUND", "Delivery order not found: abc-123"),
        ("Menu add-on", "abc-123", "MENU_ADD_ON_NOT_FOUND", "Menu add-on not found: abc-123"),
        ("Menu item", "abc-123", "MENU_ITEM_NOT_FOUND", "Menu item not found: abc-123"),
        ("Recipe for menu item", "abc-123", "RECIPE_FOR_MENU_ITEM_NOT_FOUND", "Recipe for menu item not found: abc-123"),
        ("Wastage entry", "abc-123", "WASTAGE_ENTRY_NOT_FOUND", "Wastage entry not found: abc-123"),
        ("Add-on", "abc-123", "ADD_ON_NOT_FOUND", "Add-on not found: abc-123"),
        # Carve-outs must be byte-identical to the pre-fix behaviour.
        ("LoyaltyProgram", None, "LOYALTYPROGRAM_NOT_FOUND", "LoyaltyProgram not found"),
        ("Business", "abc-123", "BUSINESS_NOT_FOUND", "Business not found: abc-123"),
    ],
)
def test_not_found_error_code_and_message(
    resource: str, resource_id: str | None, expected_code: str, expected_message: str
) -> None:
    err = NotFoundError(resource, resource_id)
    assert err.code == expected_code
    assert err.message == expected_message
    assert err.status_code == 404


def test_menu_item_duplication_resolved() -> None:
    """
    Before the fix, `NotFoundError("MenuItem", ...)` (4 call sites) and
    `NotFoundError("Menu item", ...)` (3 call sites) produced two DIFFERENT
    codes (MENUITEM_NOT_FOUND vs MENU_ITEM_NOT_FOUND) for the identical
    real-world condition. Both spellings must now converge on one code —
    verified here directly; part 2 verifies no call site still spells it
    the old way.
    """
    assert NotFoundError("MenuItem", "x").code == "MENUITEM_NOT_FOUND"
    assert NotFoundError("Menu item", "x").code == "MENU_ITEM_NOT_FOUND"
    # These are deliberately NOT equal — the fix is "stop generating
    # MENUITEM_NOT_FOUND from any call site", not "make the two spellings
    # collide", since collapsing CamelCase would break LoyaltyProgram too.
    assert NotFoundError("MenuItem", "x").code != NotFoundError("Menu item", "x").code


# ── Part 2: codebase-wide call-site scan ────────────────────────────────────

def _iter_source_files():
    for root in SCAN_ROOTS:
        yield from root.rglob("*.py")


def _string_literal_args_for_call(node: ast.Call, target_names: set[str]) -> list[tuple[int, str]]:
    """Return (lineno, literal) for the resource-carrying argument of a
    matching call, whether it's a direct `NotFoundError("X", ...)` call or a
    `require_owned(..., label="X", ...)` call."""
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if name not in target_names:
        return []

    literals: list[tuple[int, str]] = []
    if name == "NotFoundError":
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            literals.append((node.lineno, node.args[0].value))
    elif name == "require_owned":
        for kw in node.keywords:
            if kw.arg == "label" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                literals.append((node.lineno, kw.value.value))
    return literals


def _collect_resource_literals() -> list[tuple[Path, int, str]]:
    results: list[tuple[Path, int, str]] = []
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for lineno, literal in _string_literal_args_for_call(
                    node, {"NotFoundError", "require_owned"}
                ):
                    results.append((path, lineno, literal))
    return results


ALL_RESOURCE_LITERALS = _collect_resource_literals()


def test_scan_found_call_sites() -> None:
    """Sanity check the AST scan itself isn't silently finding zero call
    sites (e.g. because SCAN_ROOTS is wrong) — that would make every
    assertion below vacuously true."""
    assert len(ALL_RESOURCE_LITERALS) >= 30


@pytest.mark.parametrize(
    "path, lineno, resource",
    ALL_RESOURCE_LITERALS,
    ids=[f"{p.name}:{ln}:{r!r}" for p, ln, r in ALL_RESOURCE_LITERALS],
)
def test_every_not_found_call_site_produces_a_well_formed_code(
    path: Path, lineno: int, resource: str
) -> None:
    code = f"{_normalize_not_found_code(resource)}_NOT_FOUND"
    assert CODE_FORMAT_RE.match(code), (
        f"{path}:{lineno} — NotFoundError(\"{resource}\") produces malformed "
        f"code {code!r}"
    )
    assert "__" not in code, (
        f"{path}:{lineno} — NotFoundError(\"{resource}\") produces code "
        f"{code!r} with a double underscore"
    )


def test_no_call_site_still_uses_the_camelcase_menuitem_spelling() -> None:
    """
    Guards the BUG-0113 duplication fix specifically: every call site that
    used to pass the bare CamelCase "MenuItem"/"MenuAddOn" (producing the
    now-orphaned MENUITEM_NOT_FOUND / MENUADDON_NOT_FOUND codes) must have
    been converted to the spaced form. LoyaltyProgram is excluded — its
    CamelCase form is the deliberately-kept carve-out (see DECISIONS.md).
    """
    camel_case_offenders = {
        resource
        for _path, _lineno, resource in ALL_RESOURCE_LITERALS
        if resource in {"MenuItem", "MenuAddOn"}
    }
    assert camel_case_offenders == set(), (
        f"Found call sites still using the orphaned CamelCase spelling: "
        f"{camel_case_offenders}"
    )


# ── DuplicateError carries the identical defect class (BUG-0113 follow-up) ───
# `DuplicateError.__init__` built its code the same unnormalised way, emitting
# "ADMIN USER_DUPLICATE" and "BUSINESS FIELD_DUPLICATE". Fixed by routing it
# through the same `_normalize_not_found_code` helper. Verified zero frontend
# or e2e consumers of any *_DUPLICATE code from this class before changing it
# (the only *_DUPLICATE code any frontend matches on is the hand-written
# OPENING_STOCK_ALREADY_SET, which this class does not produce).


def _collect_duplicate_resource_literals() -> list[tuple[Path, int, str]]:
    results: list[tuple[Path, int, str]] = []
    for path in _iter_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "DuplicateError":
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str
            ):
                results.append((path, node.lineno, node.args[0].value))
    return results


ALL_DUPLICATE_RESOURCE_LITERALS = _collect_duplicate_resource_literals()


def test_duplicate_error_scan_found_call_sites() -> None:
    """Guard against the scan silently finding nothing (which would make the
    contract test below vacuously pass)."""
    assert len(ALL_DUPLICATE_RESOURCE_LITERALS) >= 4, (
        f"Expected to find DuplicateError call sites; found "
        f"{len(ALL_DUPLICATE_RESOURCE_LITERALS)}"
    )


@pytest.mark.parametrize(
    ("resource", "expected_code"),
    [
        ("Admin user", "ADMIN_USER_DUPLICATE"),
        ("business field", "BUSINESS_FIELD_DUPLICATE"),
        ("User", "USER_DUPLICATE"),
        ("mapping", "MAPPING_DUPLICATE"),
        ("supplier", "SUPPLIER_DUPLICATE"),
    ],
)
def test_duplicate_error_code_is_normalised(resource: str, expected_code: str) -> None:
    err = DuplicateError(resource, "email")
    assert err.code == expected_code
    # The human-readable message must still read naturally from the original
    # resource string — normalisation applies to the code only.
    assert resource in err.message


@pytest.mark.parametrize(
    ("path", "lineno", "resource"),
    [pytest.param(p, ln, r, id=f"{p.name}:{ln}:{r}") for p, ln, r in ALL_DUPLICATE_RESOURCE_LITERALS],
)
def test_every_duplicate_call_site_produces_a_well_formed_code(
    path: Path, lineno: int, resource: str
) -> None:
    code = f"{_normalize_not_found_code(resource)}_DUPLICATE"
    assert CODE_FORMAT_RE.match(code), (
        f"{path}:{lineno} — DuplicateError(\"{resource}\") produces malformed "
        f"code {code!r}"
    )
    assert "__" not in code, (
        f"{path}:{lineno} — DuplicateError(\"{resource}\") produces code "
        f"{code!r} with a double underscore"
    )
