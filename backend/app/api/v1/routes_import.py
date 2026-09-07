"""
CSV / XLSX import for ingredients and recipes.

PREVIEW BEFORE COMMIT
---------------------
Every import is two steps: parse and validate everything, return a preview
with per-row errors, and only write on a second explicit call. Nothing is
half-imported — a file with one bad row imports zero rows, not all-but-one.

For a franchise onboarding with hundreds of rows this matters: a silent
partial import leaves stock data that looks complete and is not, which is
worse than a failed import that says exactly which row is wrong.

UNITS ARE NEVER GUESSED
-----------------------
A row whose unit is missing or unrecognised is an error, never a default.
Guessing "150" means grams would be right most of the time and catastrophic
the rest — a wrong unit is a 1000x error that reads as theft in a variance
report.

Reuses the parsing approach already proven in
admin_api/app/services/ai_emails_import.py rather than inventing a second
import mechanism; openpyxl is already a dependency of this repo.
"""

import io
import csv
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from backend.app.inventory.units import (
    UnitError,
    cost_per_canonical_micro_cents,
    to_milli,
)
from shared.enums import CanonicalUnit
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.menu import MenuItem
from shared.models.recipe import Recipe, RecipeItem

router = APIRouter(prefix="/business/import", tags=["import"])

MAX_ROWS = 2000
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass
class RowError:
    row: int
    message: str


@dataclass
class ParseResult:
    rows: list[dict] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


async def _require_inventory_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.inventory_enabled:
        raise AppError(
            "INVENTORY_NOT_ENABLED",
            "The inventory module is not enabled for this business",
            409,
        )
    return business


async def _read_rows(upload: UploadFile) -> list[dict]:
    """Parse a .csv or .xlsx upload into a list of lower-cased-key dicts."""
    content = await upload.read()
    if len(content) > MAX_FILE_BYTES:
        raise AppError("FILE_TOO_LARGE", "File must be under 5 MB", 413)

    name = (upload.filename or "").lower()

    if name.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            for row in reader
        ]
    elif name.endswith(".xlsx"):
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        raw = list(sheet.iter_rows(values_only=True))
        if not raw:
            return []
        headers = [str(h or "").strip().lower() for h in raw[0]]
        rows = []
        for values in raw[1:]:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            rows.append(
                {
                    headers[i]: ("" if values[i] is None else str(values[i]).strip())
                    for i in range(min(len(headers), len(values)))
                }
            )
    else:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            f"Only .csv and .xlsx are accepted — got {upload.filename!r}",
            422,
        )

    if len(rows) > MAX_ROWS:
        raise AppError("TOO_MANY_ROWS", f"At most {MAX_ROWS} rows per import", 422)
    return rows


# ── Ingredients ──────────────────────────────────────────────────────────────

INGREDIENT_TEMPLATE = [
    "name", "unit", "pack_size", "pack_unit", "pack_cost", "count_method", "sku", "par_level",
]


def _parse_ingredients(rows: list[dict]) -> ParseResult:
    result = ParseResult()
    seen: set[str] = set()

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        name = (row.get("name") or "").strip()
        if not name:
            result.errors.append(RowError(index, "Missing 'name'"))
            continue
        key = name.casefold()
        if key in seen:
            result.errors.append(RowError(index, f"Duplicate name {name!r} in this file"))
            continue
        seen.add(key)

        unit_raw = (row.get("unit") or "").strip().upper()
        canonical = {
            "G": "GRAM", "GRAM": "GRAM", "GRAMS": "GRAM", "KG": "GRAM",
            "ML": "MILLILITRE", "MILLILITRE": "MILLILITRE", "L": "MILLILITRE",
            "UNIT": "UNIT", "UNITS": "UNIT", "EACH": "UNIT",
        }.get(unit_raw)
        if not canonical:
            result.errors.append(
                RowError(index, f"'unit' is {unit_raw or 'empty'} — use g, kg, ml, L or unit")
            )
            continue

        pack_size = (row.get("pack_size") or "1").strip() or "1"
        pack_unit = (row.get("pack_unit") or unit_raw or "UNIT").strip().upper()
        try:
            pack_milli = to_milli(pack_size, pack_unit, canonical=CanonicalUnit(canonical))
        except (UnitError, ValueError) as exc:
            result.errors.append(RowError(index, f"pack_size/pack_unit: {exc}"))
            continue
        if pack_milli <= 0:
            result.errors.append(RowError(index, "pack_size must be greater than zero"))
            continue

        pack_cost_cents = None
        raw_cost = (row.get("pack_cost") or "").strip().replace("R", "").replace(",", "")
        if raw_cost:
            try:
                pack_cost_cents = int(round(float(raw_cost) * 100))
            except ValueError:
                result.errors.append(RowError(index, f"pack_cost {raw_cost!r} is not a number"))
                continue

        par_milli = None
        raw_par = (row.get("par_level") or "").strip()
        if raw_par:
            try:
                par_milli = to_milli(raw_par, pack_unit, canonical=CanonicalUnit(canonical))
            except (UnitError, ValueError) as exc:
                result.errors.append(RowError(index, f"par_level: {exc}"))
                continue

        count_method = (row.get("count_method") or "").strip().upper()
        if count_method not in ("UNIT_COUNT", "WEIGHT", "VOLUME"):
            count_method = {
                "GRAM": "WEIGHT", "MILLILITRE": "VOLUME", "UNIT": "UNIT_COUNT"
            }[canonical]

        result.rows.append({
            "name": name,
            "canonical_unit": canonical,
            "count_method": count_method,
            "sku": (row.get("sku") or "").strip() or None,
            "purchase_unit_name": f"{pack_size} {pack_unit.lower()}" if pack_size != "1" else None,
            "purchase_pack_milli": pack_milli,
            "cost_per_pack_cents": pack_cost_cents,
            "cost_per_canonical_micro_cents": (
                cost_per_canonical_micro_cents(pack_cost_cents, pack_milli)
                if pack_cost_cents else 0
            ),
            "par_level_milli": par_milli,
        })

    return result


@router.post("/ingredients/preview")
async def preview_ingredients(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse and validate without writing anything.

    Also flags names that already exist, so an operator can see what would be
    skipped before committing.
    """
    await _require_inventory_enabled(db, user.business_id)
    parsed = _parse_ingredients(await _read_rows(file))

    existing = {
        name.casefold()
        for (name,) in (
            await db.execute(
                select(Ingredient.name).where(
                    Ingredient.business_id == user.business_id,
                    Ingredient.is_deleted.is_(False),
                )
            )
        ).all()
    }

    return {
        "template_columns": INGREDIENT_TEMPLATE,
        "total_rows": len(parsed.rows) + len(parsed.errors),
        "valid_rows": len(parsed.rows),
        "error_count": len(parsed.errors),
        "errors": [{"row": e.row, "message": e.message} for e in parsed.errors],
        "would_create": [r["name"] for r in parsed.rows if r["name"].casefold() not in existing],
        "would_skip_existing": [r["name"] for r in parsed.rows if r["name"].casefold() in existing],
        "preview": parsed.rows[:20],
    }


@router.post("/ingredients/commit")
async def commit_ingredients(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Import ingredients. Refuses the whole file if ANY row is invalid, so a
    partial import can never leave stock data that looks complete but is not.
    """
    await _require_inventory_enabled(db, user.business_id)
    parsed = _parse_ingredients(await _read_rows(file))

    if parsed.errors:
        raise AppError(
            "IMPORT_HAS_ERRORS",
            f"{len(parsed.errors)} row(s) have problems — nothing was imported",
            422,
            details={"errors": [{"row": e.row, "message": e.message} for e in parsed.errors[:50]]},
        )

    existing = {
        name.casefold()
        for (name,) in (
            await db.execute(
                select(Ingredient.name).where(
                    Ingredient.business_id == user.business_id,
                    Ingredient.is_deleted.is_(False),
                )
            )
        ).all()
    }

    created = 0
    skipped = []
    for row in parsed.rows:
        if row["name"].casefold() in existing:
            skipped.append(row["name"])
            continue
        db.add(Ingredient(business_id=user.business_id, **row))
        created += 1

    await db.commit()
    return {"created": created, "skipped_existing": skipped}


# ── Recipes ──────────────────────────────────────────────────────────────────

RECIPE_TEMPLATE = ["menu_item", "ingredient", "quantity", "unit", "option_id", "removable_id"]


async def _load_lookup(db: AsyncSession, business_id: uuid.UUID):
    """Menu items and ingredients for this tenant, keyed case-insensitively."""
    items = {
        item.name.casefold(): item
        for item in (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.business_id == business_id,
                    MenuItem.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    }
    ingredients = {
        ing.name.casefold(): ing
        for ing in (
            await db.execute(
                select(Ingredient).where(
                    Ingredient.business_id == business_id,
                    Ingredient.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    }
    return items, ingredients


def _parse_recipes(rows: list[dict], items: dict, ingredients: dict):
    """
    One parser used by BOTH preview and commit, so the two can never disagree
    about what a file means.

    Returns (grouped_by_menu_item_id, errors). Names are matched
    case-insensitively; anything unmatched is an error rather than a silently
    dropped line, because a missing recipe line is invisible in the UI but
    permanently understates usage.
    """
    errors: list[RowError] = []
    grouped: dict[str, list[dict]] = {}

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        item_name = (row.get("menu_item") or "").strip()
        ingredient_name = (row.get("ingredient") or "").strip()
        quantity = (row.get("quantity") or "").strip()
        unit = (row.get("unit") or "").strip()

        if not item_name or not ingredient_name:
            errors.append(RowError(index, "Both 'menu_item' and 'ingredient' are required"))
            continue
        item = items.get(item_name.casefold())
        if not item:
            errors.append(RowError(index, f"No menu item named {item_name!r}"))
            continue
        ingredient = ingredients.get(ingredient_name.casefold())
        if not ingredient:
            errors.append(RowError(index, f"No ingredient named {ingredient_name!r}"))
            continue
        if not unit:
            # Never guessed - see the module docstring.
            errors.append(RowError(index, "'unit' is required; it is never assumed"))
            continue
        try:
            quantity_milli = to_milli(
                quantity, unit, canonical=CanonicalUnit(ingredient.canonical_unit)
            )
        except (UnitError, ValueError) as exc:
            errors.append(RowError(index, f"{ingredient_name}: {exc}"))
            continue
        if quantity_milli <= 0:
            errors.append(
                RowError(index, f"{ingredient_name}: quantity must be greater than zero")
            )
            continue

        grouped.setdefault(str(item.id), []).append({
            "menu_item_id": str(item.id),
            "menu_item_name": item.name,
            "ingredient_id": str(ingredient.id),
            "ingredient_name": ingredient.name,
            "quantity_milli": quantity_milli,
            "option_id": (row.get("option_id") or "").strip() or None,
            "removable_ref_id": (row.get("removable_id") or "").strip() or None,
        })

    return grouped, errors


@router.post("/recipes/preview")
async def preview_recipes(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Validate a recipe sheet against the live menu and ingredient list without
    writing anything. One row per ingredient line, grouped by menu_item.
    """
    await _require_inventory_enabled(db, user.business_id)
    rows = await _read_rows(file)
    items, ingredients = await _load_lookup(db, user.business_id)
    grouped, errors = _parse_recipes(rows, items, ingredients)

    return {
        "template_columns": RECIPE_TEMPLATE,
        "total_rows": len(rows),
        "valid_rows": sum(len(v) for v in grouped.values()),
        "error_count": len(errors),
        "errors": [{"row": e.row, "message": e.message} for e in errors],
        "recipes": [
            {"menu_item_id": k, "menu_item_name": v[0]["menu_item_name"], "lines": len(v)}
            for k, v in grouped.items()
        ],
        "preview": [line for lines in grouped.values() for line in lines][:20],
    }


@router.post("/recipes/commit")
async def commit_recipes(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Import recipes, replacing the active recipe for each menu item in the
    file. Refuses the whole file if any row is invalid.

    Superseded recipes are deactivated rather than deleted, and replacing a
    recipe never rewrites history: consumption is snapshotted onto the ledger
    at sale time, so past orders keep exactly what they consumed.
    """
    await _require_inventory_enabled(db, user.business_id)
    rows = await _read_rows(file)
    items, ingredients = await _load_lookup(db, user.business_id)
    grouped, errors = _parse_recipes(rows, items, ingredients)

    if errors:
        raise AppError(
            "IMPORT_HAS_ERRORS",
            f"{len(errors)} row(s) have problems - nothing was imported",
            422,
            details={"errors": [{"row": e.row, "message": e.message} for e in errors[:50]]},
        )
    if not grouped:
        raise AppError("EMPTY_IMPORT", "No usable recipe rows were found in that file", 422)

    ingredient_by_id = {str(i.id): i for i in ingredients.values()}
    recipes_written = 0

    for item_id, lines in grouped.items():
        existing = (
            await db.execute(
                select(Recipe).where(
                    Recipe.business_id == user.business_id,
                    Recipe.menu_item_id == uuid.UUID(item_id),
                    Recipe.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()

        next_version = (existing.version + 1) if existing else 1
        if existing:
            # Deactivate first: a partial unique index allows only one active
            # recipe per menu item, so the flush order matters here.
            existing.is_active = False
            await db.flush()

        recipe = Recipe(
            business_id=user.business_id,
            menu_item_id=uuid.UUID(item_id),
            version=next_version,
            yield_qty=1,
        )
        db.add(recipe)
        await db.flush()

        for order, line in enumerate(lines):
            db.add(
                RecipeItem(
                    business_id=user.business_id,
                    recipe_id=recipe.id,
                    ingredient_id=ingredient_by_id[line["ingredient_id"]].id,
                    quantity_milli=line["quantity_milli"],
                    option_id=line["option_id"],
                    removable_ref_id=line["removable_ref_id"],
                    sort_order=order,
                )
            )
        recipes_written += 1

    await db.commit()
    return {
        "recipes_written": recipes_written,
        "lines": sum(len(v) for v in grouped.values()),
    }
