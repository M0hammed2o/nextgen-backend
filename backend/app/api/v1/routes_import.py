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
import json
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, File, Form, UploadFile
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
from shared.models.menu import MenuCategory, MenuItem
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


def apply_mapping(rows: list[dict], mapping: dict | None) -> list[dict]:
    """
    Rename a spreadsheet's own column headings to the ones the parsers expect.

    A restaurant's sheet says "Product", "Selling Price", "Item Description".
    Rather than forcing them to rewrite the file, the UI shows the detected
    headings, the user maps each to a known field, and that mapping arrives
    here as {"their heading": "our field"}.

    Unmapped columns are kept as-is, so a sheet that already uses the right
    headings needs no mapping at all.
    """
    if not mapping:
        return rows
    lookup = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in mapping.items()
        if k and v
    }
    if not lookup:
        return rows
    return [{lookup.get(k, k): v for k, v in row.items()} for row in rows]


def detected_columns(rows: list[dict]) -> list[str]:
    """The headings actually present, so the UI can offer a mapping step."""
    seen: list[str] = []
    for row in rows[:5]:
        for key in row:
            if key and key not in seen:
                seen.append(key)
    return seen


def parse_mapping(raw: str | None) -> dict | None:
    """Mapping arrives as a JSON string alongside a multipart file upload."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AppError("INVALID_MAPPING", "Column mapping must be valid JSON", 422) from exc
    if not isinstance(parsed, dict):
        raise AppError("INVALID_MAPPING", "Column mapping must be an object", 422)
    return parsed


# -- Menu items --------------------------------------------------------------

MENU_TEMPLATE = [
    "category", "name", "description", "price", "size", "active", "sku",
]


def _money_to_cents(raw: str) -> int | None:
    cleaned = (raw or "").strip().replace("R", "").replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    return int(round(float(cleaned) * 100))


def _parse_menu(rows: list[dict]) -> tuple[dict, list[RowError]]:
    """
    Parse a menu sheet into grouped items.

    SIZES ARE NOT SEPARATE PRODUCTS. This platform models a size as an option
    group on ONE menu item, and the recipe system scopes ingredient quantities
    to those options - that is what lets a Large deduct more cheese than a
    Small. So rows sharing a category+name are collapsed into one item with a
    Size option group, priced as deltas from the cheapest size.

    Returns ({(category, name): item_dict}, errors).
    """
    grouped: dict[tuple, dict] = {}
    errors: list[RowError] = []

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        name = (row.get("name") or row.get("product") or row.get("item") or "").strip()
        if not name:
            errors.append(RowError(index, "Missing 'name'"))
            continue

        category = (row.get("category") or "").strip() or "Menu"
        size = (row.get("size") or "").strip()

        raw_price = row.get("price") or row.get("selling price") or ""
        try:
            price_cents = _money_to_cents(str(raw_price))
        except ValueError:
            errors.append(RowError(index, f"Price {raw_price!r} is not a number"))
            continue
        if price_cents is None:
            errors.append(RowError(index, f"{name}: price is missing"))
            continue
        if price_cents < 0:
            errors.append(RowError(index, f"{name}: price cannot be negative"))
            continue

        active_raw = (row.get("active") or "").strip().lower()
        is_active = active_raw not in ("no", "false", "0", "inactive", "n")

        key = (category.casefold(), name.casefold())
        entry = grouped.setdefault(key, {
            "category": category,
            "name": name,
            "description": (row.get("description") or "").strip() or None,
            "sku": (row.get("sku") or row.get("code") or "").strip() or None,
            "is_active": is_active,
            "sizes": [],
            "price_cents": None,
            "rows": [],
        })
        entry["rows"].append(index)

        if size:
            if any(s["name"].casefold() == size.casefold() for s in entry["sizes"]):
                errors.append(
                    RowError(index, f"Duplicate size {size!r} for {name!r}")
                )
                continue
            entry["sizes"].append({"name": size, "price_cents": price_cents})
        else:
            if entry["price_cents"] is not None:
                errors.append(RowError(index, f"Duplicate menu item {name!r}"))
                continue
            entry["price_cents"] = price_cents

    # Resolve base price + option deltas now that every row is seen.
    for key, entry in grouped.items():
        if entry["sizes"]:
            base = min(s["price_cents"] for s in entry["sizes"])
            entry["price_cents"] = base
            for size in entry["sizes"]:
                size["price_delta_cents"] = size["price_cents"] - base
        elif entry["price_cents"] is None:
            errors.append(RowError(entry["rows"][0], f"{entry['name']}: no price"))

    return grouped, errors


@router.post("/menu/preview")
async def preview_menu(
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse and validate a menu spreadsheet without writing anything.

    Returns the detected column headings so the UI can offer a mapping step,
    plus row-level errors and exactly what would be created or updated.
    """
    raw_rows = await _read_rows(file)
    rows = apply_mapping(raw_rows, parse_mapping(mapping))
    grouped, errors = _parse_menu(rows)

    existing = {
        (item.name or "").casefold()
        for item in (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.business_id == user.business_id,
                    MenuItem.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    }

    preview = []
    for entry in grouped.values():
        preview.append({
            "category": entry["category"],
            "name": entry["name"],
            "description": entry["description"],
            "price_cents": entry["price_cents"],
            "sizes": [
                {"name": s["name"], "price_cents": s["price_cents"],
                 "price_delta_cents": s.get("price_delta_cents", 0)}
                for s in entry["sizes"]
            ],
            "is_active": entry["is_active"],
            "action": "update" if entry["name"].casefold() in existing else "create",
        })

    return {
        "template_columns": MENU_TEMPLATE,
        "detected_columns": detected_columns(raw_rows),
        "total_rows": len(rows),
        "valid_items": len(grouped),
        "error_count": len(errors),
        "errors": [{"row": e.row, "message": e.message} for e in errors],
        "would_create": sum(1 for p in preview if p["action"] == "create"),
        "would_update": sum(1 for p in preview if p["action"] == "update"),
        "preview": preview,
    }


@router.post("/menu/commit")
async def commit_menu(
    file: UploadFile = File(...),
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Import a menu. Refuses the whole file if ANY row is invalid, so a partial
    menu can never go live looking complete.

    Existing items with the same name are UPDATED in place rather than
    duplicated, so re-uploading a corrected sheet is safe and repeatable -
    which is the whole point of letting a restaurant fix and re-upload.
    """
    raw_rows = await _read_rows(file)
    rows = apply_mapping(raw_rows, parse_mapping(mapping))
    grouped, errors = _parse_menu(rows)

    if errors:
        raise AppError(
            "IMPORT_HAS_ERRORS",
            f"{len(errors)} row(s) have problems - nothing was imported",
            422,
            details={"errors": [{"row": e.row, "message": e.message} for e in errors[:50]]},
        )
    if not grouped:
        raise AppError("EMPTY_IMPORT", "No usable menu rows were found in that file", 422)

    categories = {
        (c.name or "").casefold(): c
        for c in (
            await db.execute(
                select(MenuCategory).where(MenuCategory.business_id == user.business_id)
            )
        ).scalars().all()
    }
    items = {
        (i.name or "").casefold(): i
        for i in (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.business_id == user.business_id,
                    MenuItem.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    }

    created = updated = categories_created = 0

    for entry in grouped.values():
        cat_key = entry["category"].casefold()
        category = categories.get(cat_key)
        if not category:
            category = MenuCategory(business_id=user.business_id, name=entry["category"])
            db.add(category)
            await db.flush()
            categories[cat_key] = category
            categories_created += 1

        options_json = None
        if entry["sizes"]:
            options_json = {
                "option_groups": [{
                    "id": "size",
                    "name": "Size",
                    "required": True,
                    "is_enabled": True,
                    "options": [
                        {
                            # Stable, human-readable ids so a recipe sheet can
                            # reference "size-large" without the manager
                            # hunting for a UUID.
                            "id": f"size-{s['name'].strip().lower().replace(' ', '-')}",
                            "name": s["name"],
                            "price_delta_cents": s["price_delta_cents"],
                            "is_enabled": True,
                        }
                        for s in entry["sizes"]
                    ],
                }],
                "removable_ingredients": [],
            }

        item = items.get(entry["name"].casefold())
        if item:
            item.category_id = category.id
            item.name = entry["name"]
            item.description = entry["description"]
            item.price_cents = entry["price_cents"]
            item.is_active = entry["is_active"]
            if options_json:
                # Preserve any removable ingredients the manager configured by
                # hand; only the size group is replaced from the sheet.
                previous = item.options_json or {}
                options_json["removable_ingredients"] = previous.get(
                    "removable_ingredients", []
                ) or []
                item.options_json = options_json
            updated += 1
        else:
            db.add(MenuItem(
                business_id=user.business_id,
                category_id=category.id,
                name=entry["name"],
                description=entry["description"],
                price_cents=entry["price_cents"],
                is_active=entry["is_active"],
                options_json=options_json,
            ))
            created += 1

    await db.commit()
    return {
        "created": created,
        "updated": updated,
        "categories_created": categories_created,
    }


# ── Ingredients ──────────────────────────────────────────────────────────────

INGREDIENT_TEMPLATE = [
    "name", "unit", "unit_label", "category", "pack_size", "pack_unit",
    "pack_cost", "count_method", "sku", "par_level", "opening_quantity",
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
            "UNIT": "UNIT", "UNITS": "UNIT", "EACH": "UNIT", "EA": "UNIT",
            "PORTION": "UNIT", "PORTIONS": "UNIT", "SACHET": "UNIT",
            "SACHETS": "UNIT", "BOTTLE": "UNIT", "BOTTLES": "UNIT",
            "BOX": "UNIT", "BOXES": "UNIT", "BALL": "UNIT", "BALLS": "UNIT",
            "TUB": "UNIT", "SLICE": "UNIT", "SLICES": "UNIT",
            "CAN": "UNIT", "CANS": "UNIT",
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

        # The noun staff use. Defaults to whatever countable word they typed
        # in the unit column ("portion", "box"), so a franchise sheet needs no
        # extra column to read naturally on screen.
        unit_label = (row.get("unit_label") or "").strip() or None
        if not unit_label and canonical == "UNIT" and unit_raw not in ("UNIT", "UNITS", ""):
            unit_label = unit_raw.lower().rstrip("s") or None

        result.rows.append({
            "name": name,
            "canonical_unit": canonical,
            "count_method": count_method,
            "unit_label": unit_label,
            "category": (row.get("category") or "").strip() or None,
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
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Parse and validate without writing anything.

    Also flags names that already exist, so an operator can see what would be
    skipped before committing.
    """
    await _require_inventory_enabled(db, user.business_id)
    raw_rows = await _read_rows(file)
    parsed = _parse_ingredients(apply_mapping(raw_rows, parse_mapping(mapping)))

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
        "detected_columns": detected_columns(raw_rows),
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
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Import ingredients. Refuses the whole file if ANY row is invalid, so a
    partial import can never leave stock data that looks complete but is not.
    """
    await _require_inventory_enabled(db, user.business_id)
    raw_rows = await _read_rows(file)
    parsed = _parse_ingredients(apply_mapping(raw_rows, parse_mapping(mapping)))

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
    mapping: str | None = Form(default=None),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Validate a recipe sheet against the live menu and ingredient list without
    writing anything. One row per ingredient line, grouped by menu_item.
    """
    await _require_inventory_enabled(db, user.business_id)
    raw_rows = await _read_rows(file)
    rows = apply_mapping(raw_rows, parse_mapping(mapping))
    items, ingredients = await _load_lookup(db, user.business_id)
    grouped, errors = _parse_recipes(rows, items, ingredients)

    return {
        "template_columns": RECIPE_TEMPLATE,
        "detected_columns": detected_columns(raw_rows),
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
    mapping: str | None = Form(default=None),
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
    raw_rows = await _read_rows(file)
    rows = apply_mapping(raw_rows, parse_mapping(mapping))
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
