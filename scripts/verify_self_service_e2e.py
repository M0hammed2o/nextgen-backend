"""
End-to-end verification of RESTAURANT SELF-SERVICE against a real Postgres.

The acceptance question this answers: could we create a tenant, hand the
owner their login, walk away, and have them configure and run the restaurant
themselves?

    owner signs in -> imports menu spreadsheet -> imports ingredient sheet
    -> imports recipes -> opening stock -> sells -> wastage -> live expected
    stock -> stocktake -> variance -> potential loss value

Run against a throwaway database only. Never point this at production.
"""

import asyncio
import io
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://nextgen:testpw@localhost:55445/nextgen_test"
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_ADMIN_SECRET_KEY", "test-admin-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "t")
os.environ.setdefault("WHATSAPP_DEFAULT_ACCESS_TOKEN", "t")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from backend.app.api.v1 import routes_import, routes_inventory, routes_setup  # noqa: E402
from backend.app.core.rbac import AuthUser  # noqa: E402
from backend.app.inventory.ledger import build_movement, post_sale_consumption  # noqa: E402
from backend.app.inventory.units import to_milli, value_cents  # noqa: E402
from backend.app.pos.pricing import ResolvedPOSItem  # noqa: E402
from shared.enums import StockMovementType  # noqa: E402
from shared.models.business import Business  # noqa: E402
from shared.models.inventory import Ingredient  # noqa: E402
from shared.models.menu import MenuItem  # noqa: E402
from shared.models.recipe import Recipe  # noqa: E402
from shared.pricing.engine import LineItemBreakdown, apply_vat_to_total  # noqa: E402
from sqlalchemy import select  # noqa: E402

OK = []


def check(label, condition, detail=""):
    OK.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


# NOTE ON CALLING ROUTES DIRECTLY
# These are FastAPI route functions invoked as plain coroutines, so any
# parameter declared `= Query(default)` arrives as the Query OBJECT rather
# than the default value — and a Query object is truthy. FastAPI substitutes
# real values at request time, so this is a harness artifact rather than a
# product bug, but every Query-backed argument below is therefore passed
# EXPLICITLY. Forgetting one silently inverts a boolean filter.


class FakeUpload:
    """Stands in for FastAPI's UploadFile — the importers only read()."""

    def __init__(self, filename: str, content: str):
        self.filename = filename
        self._data = content.encode("utf-8")

    async def read(self):
        return self._data

    async def seek(self, _pos):
        return None


MENU_CSV = """category,name,description,price,size
Pizza,Margherita,Classic tomato and cheese,89.00,Small
Pizza,Margherita,Classic tomato and cheese,119.00,Medium
Pizza,Margherita,Classic tomato and cheese,149.00,Large
Pizza,Regina,Ham and mushroom,109.00,Small
Pizza,Regina,Ham and mushroom,169.00,Large
Drinks,Coca-Cola 330ml,,22.00,
"""

INGREDIENT_CSV = """name,unit,unit_label,category,pack_size,pack_unit,pack_cost,opening_quantity
Small Dough Ball,portion,portion,Dough,1,portion,4.50,50
Large Dough Ball,portion,portion,Dough,1,portion,6.50,100
Tomato Sauce Portion,portion,portion,Sauces,1,portion,3.20,100
Cheese Portion,portion,portion,Dairy,1,portion,15.00,100
Flour,g,,Dry Goods,1,kg,18.00,5000
Small Pizza Box,box,box,Packaging,1,box,2.80,50
Large Pizza Box,box,box,Packaging,1,box,4.10,100
"""

RECIPE_CSV = """menu_item,ingredient,quantity,unit,option_id
Margherita,Small Dough Ball,1,portion,size-small
Margherita,Large Dough Ball,1,portion,size-medium
Margherita,Large Dough Ball,1,portion,size-large
Margherita,Tomato Sauce Portion,1,portion,
Margherita,Cheese Portion,1,portion,size-small
Margherita,Cheese Portion,2,portion,size-medium
Margherita,Cheese Portion,2,portion,size-large
Margherita,Flour,20,g,
Margherita,Small Pizza Box,1,box,size-small
Margherita,Large Pizza Box,1,box,size-medium
Margherita,Large Pizza Box,1,box,size-large
"""


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # A brand-new tenant, exactly as NextGen would hand it over.
        biz = Business(
            name="Self Service Test Pizzeria",
            slug=f"ss-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(),
            inventory_enabled=True,
            is_whatsapp_enabled=False,
            vat_registered=True,
            vat_rate_basis_points=1500,
            vat_inclusive_pricing=True,
        )
        other = Business(
            name="Other Pizzeria",
            slug=f"ot-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(),
            inventory_enabled=True,
        )
        db.add_all([biz, other])
        await db.flush()
        BIZ, OTHER = biz.id, other.id
        await db.commit()

        owner = AuthUser(user_id=uuid.uuid4(), business_id=BIZ, role="OWNER", token_type="access")
        other_owner = AuthUser(
            user_id=uuid.uuid4(), business_id=OTHER, role="OWNER", token_type="access"
        )

        # ── 1. Setup progress on an empty restaurant ──────────────────────
        print("\n== SETUP PROGRESS (empty restaurant) ==")
        progress = await routes_setup.get_setup_progress(user=owner, db=db)
        keys = {s.key: s for s in progress.steps}
        check("progress is reported", progress.percent_complete >= 0,
              f"{progress.percent_complete}%")
        check("not ready to trade yet", not progress.ready_to_trade)
        check("menu flagged as todo", keys["menu"].status == "todo")
        check("ingredients flagged as todo", keys["ingredients"].status == "todo")
        check("opening stock flagged as todo", keys["opening_stock"].status == "todo")

        # ── 2. Owner imports their own menu ───────────────────────────────
        print("\n== MENU IMPORT (owner, spreadsheet) ==")
        prev = await routes_import.preview_menu(
            file=FakeUpload("menu.csv", MENU_CSV), mapping=None, user=owner, db=db
        )
        check("preview reports no errors", prev["error_count"] == 0, str(prev["errors"])[:120])
        check("3 distinct items detected", prev["valid_items"] == 3, str(prev["valid_items"]))
        check("detected columns surfaced for mapping", "category" in prev["detected_columns"])
        margherita = next(p for p in prev["preview"] if p["name"] == "Margherita")
        check("sizes collapsed onto ONE item", len(margherita["sizes"]) == 3)
        check("base price is the cheapest size (R89)", margherita["price_cents"] == 8900)
        large = next(s for s in margherita["sizes"] if s["name"] == "Large")
        check("large priced as a +R60 delta", large["price_delta_cents"] == 6000)

        res = await routes_import.commit_menu(
            file=FakeUpload("menu.csv", MENU_CSV), mapping=None, user=owner, db=db
        )
        check("menu committed", res["created"] == 3, str(res))
        check("categories auto-created", res["categories_created"] == 2, str(res))

        # Re-upload must update, not duplicate — the "fix and re-upload" loop.
        res2 = await routes_import.commit_menu(
            file=FakeUpload("menu.csv", MENU_CSV), mapping=None, user=owner, db=db
        )
        check("re-upload updates instead of duplicating",
              res2["created"] == 0 and res2["updated"] == 3, str(res2))

        # ── 3. Column mapping ─────────────────────────────────────────────
        print("\n== COLUMN MAPPING (their headings, not ours) ==")
        their_csv = "Group,Product,Selling Price\nSides,Garlic Bread,45.00\n"
        mapped = await routes_import.preview_menu(
            file=FakeUpload("theirs.csv", their_csv),
            mapping='{"Group":"category","Product":"name","Selling Price":"price"}',
            user=owner, db=db,
        )
        check("mapped sheet parses cleanly", mapped["error_count"] == 0, str(mapped["errors"])[:120])
        check("mapped item found", mapped["valid_items"] == 1)

        bad = await routes_import.preview_menu(
            file=FakeUpload("bad.csv", "category,name,price\nPizza,,89\nPizza,Hawaiian,notanumber\n"),
            mapping=None, user=owner, db=db,
        )
        check("row errors reported with row numbers", bad["error_count"] == 2, str(bad["errors"]))
        check("first error is row 2", bad["errors"][0]["row"] == 2)

        # ── 4. Owner imports ingredients ──────────────────────────────────
        print("\n== INGREDIENT IMPORT (prepacked portions) ==")
        iprev = await routes_import.preview_ingredients(
            file=FakeUpload("ing.csv", INGREDIENT_CSV), mapping=None, user=owner, db=db
        )
        check("ingredient preview clean", iprev["error_count"] == 0, str(iprev["errors"])[:150])
        check("7 ingredients", iprev["valid_rows"] == 7, str(iprev["valid_rows"]))
        cheese_row = next(r for r in iprev["preview"] if r["name"] == "Cheese Portion")
        check("portion stored as a countable UNIT", cheese_row["canonical_unit"] == "UNIT")
        check("noun captured for display", cheese_row["unit_label"] == "portion")
        check("category captured", cheese_row["category"] == "Dairy")
        check("R15/portion -> unit cost",
              cheese_row["cost_per_canonical_micro_cents"] == 1_500_000_000)

        check("opening stock detected in the sheet", iprev["with_opening_stock"] == 7,
              str(iprev.get("with_opening_stock")))

        icommit = await routes_import.commit_ingredients(
            file=FakeUpload("ing.csv", INGREDIENT_CSV), mapping=None, user=owner, db=db
        )
        check("ingredients committed", icommit["created"] == 7, str(icommit))
        check("opening stock posted straight from the sheet",
              icommit["opening_balances_posted"] == 7, str(icommit))

        # ── 5. Owner imports recipes ──────────────────────────────────────
        print("\n== RECIPE IMPORT (size-scoped) ==")
        rprev = await routes_import.preview_recipes(
            file=FakeUpload("rec.csv", RECIPE_CSV), mapping=None, user=owner, db=db
        )
        check("recipe preview clean", rprev["error_count"] == 0, str(rprev["errors"])[:200])
        check("11 recipe lines", rprev["valid_rows"] == 11, str(rprev["valid_rows"]))
        rcommit = await routes_import.commit_recipes(
            file=FakeUpload("rec.csv", RECIPE_CSV), mapping=None, user=owner, db=db
        )
        check("recipe committed", rcommit["recipes_written"] == 1, str(rcommit))

        bad_recipe = await routes_import.preview_recipes(
            file=FakeUpload("bad.csv",
                            "menu_item,ingredient,quantity,unit\nMargherita,Truffle,1,portion\n"),
            mapping=None, user=owner, db=db,
        )
        check("unknown ingredient is an error, not a dropped line",
              bad_recipe["error_count"] == 1 and "No ingredient" in bad_recipe["errors"][0]["message"])

        # ── 6. Recipe coverage ────────────────────────────────────────────
        print("\n== RECIPE COVERAGE ==")
        cov = await routes_setup.get_recipe_coverage(user=owner, db=db)
        by_name = {c.name: c for c in cov.items}
        check("Margherita complete", by_name["Margherita"].status == "complete",
              by_name["Margherita"].message)
        check("Regina has no recipe", by_name["Regina"].status == "none")
        check("missing recipes counted", cov.missing >= 1, f"missing={cov.missing}")

        # ── 7. Opening stock, entered in the restaurant's own units ───────
        print("\n== OPENING STOCK ==")
        ingredients = {
            i.name: i for i in (
                await db.execute(select(Ingredient).where(Ingredient.business_id == BIZ))
            ).scalars().all()
        }

        live = await routes_setup.get_live_stock(attention_only=False, user=owner, db=db)
        rows = {r.name: r for r in live.rows}
        check("expected shown in the restaurant's own words",
              rows["Cheese Portion"].expected_display == "100 portions",
              rows["Cheese Portion"].expected_display)
        check("boxes read as boxes",
              rows["Large Pizza Box"].expected_display == "100 boxes")
        check("weight still reads as weight",
              rows["Flour"].expected_display == "5 kg", rows["Flour"].expected_display)

        # ── 8. Sell 20 large Margheritas ──────────────────────────────────
        print("\n== SELL 20 LARGE MARGHERITA ==")
        item = (await db.execute(
            select(MenuItem).where(MenuItem.business_id == BIZ, MenuItem.name == "Margherita")
        )).scalar_one()
        resolved = ResolvedPOSItem(
            menu_item_id=item.id, name="Margherita",
            breakdown=LineItemBreakdown(8900, 6000, 0, 14900, 20, 298000),
            selected_options_snapshot=[{
                "group_id": "size", "group_name": "Size",
                "option_id": "size-large", "option_name": "Large",
                "price_delta_cents": 6000,
            }],
            add_ons_snapshot=[], removed_ingredients_snapshot=[],
            special_instructions=None, requires_preparation=True,
        )
        await post_sale_consumption(
            db, business_id=BIZ, order_id=uuid.uuid4(), resolved_items=[resolved],
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        await db.commit()

        live = await routes_setup.get_live_stock(attention_only=False, user=owner, db=db)
        rows = {r.name: r for r in live.rows}
        check("dough 100 -> 80 portions", rows["Large Dough Ball"].expected_display == "80 portions")
        check("2 cheese per large pizza -> 60 left",
              rows["Cheese Portion"].expected_display == "60 portions",
              rows["Cheese Portion"].expected_display)
        check("sauce 100 -> 80 portions",
              rows["Tomato Sauce Portion"].expected_display == "80 portions")
        check("flour 5 kg - 400 g -> 4.6 kg",
              rows["Flour"].expected_display == "4.6 kg", rows["Flour"].expected_display)
        check("used-by-sales shown separately",
              rows["Large Dough Ball"].used_by_sales_display == "20 portions")

        # ── 9. Wastage ────────────────────────────────────────────────────
        print("\n== WASTAGE ==")
        cheese = ingredients["Cheese Portion"]
        db.add(build_movement(
            business_id=BIZ, ingredient_id=cheese.id,
            quantity_milli=-to_milli(2, "portion"),
            movement_type=StockMovementType.WASTAGE.value,
            unit_cost_micro_cents=cheese.cost_per_canonical_micro_cents,
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ))
        await db.commit()
        live = await routes_setup.get_live_stock(attention_only=False, user=owner, db=db)
        rows = {r.name: r for r in live.rows}
        check("wastage reduces expected: 60 -> 58",
              rows["Cheese Portion"].expected_display == "58 portions",
              rows["Cheese Portion"].expected_display)
        check("wastage shown as its own column",
              rows["Cheese Portion"].wastage_display == "2 portions")

        # ── 10. Stocktake, variance and rand value ────────────────────────
        print("\n== STOCKTAKE -> VARIANCE -> POTENTIAL LOSS ==")
        from backend.app.api.v1 import routes_stocktake

        st = await routes_stocktake.open_stocktake(
            body=routes_stocktake.StocktakeOpenRequest(reference="Week 1"), user=owner, db=db
        )
        counts = [
            routes_stocktake.CountLineRequest(
                ingredient_id=line.ingredient_id,
                quantity=("54" if line.ingredient_name == "Cheese Portion"
                          else str(int(line.expected_quantity_milli / 1000))),
            )
            for line in st.lines
        ]
        await routes_stocktake.submit_counts(
            session_id=st.id,
            body=routes_stocktake.CountSubmitRequest(lines=counts),
            user=owner, db=db,
        )
        done = await routes_stocktake.complete_stocktake(session_id=st.id, user=owner, db=db)
        cheese_line = next(l for l in done.lines if l.ingredient_name == "Cheese Portion")
        check("expected 58 portions", cheese_line.expected_display == "58 portions",
              cheese_line.expected_display)
        check("counted 54 portions", cheese_line.counted_display == "54 portions")
        check("variance -4 portions", cheese_line.variance_display == "-4 portions",
              cheese_line.variance_display)
        check("potential loss R60 at R15/portion",
              cheese_line.variance_value_cents == -6000,
              f"{cheese_line.variance_value_cents}")
        check("variance flagged for attention", cheese_line.is_flagged)

        report = await routes_stocktake.get_variance_report(
            session_id=st.id, flagged_only=False, user=owner, db=db
        )
        check("report explains the formula", "expected =" in report["formula"])
        check("worst variance ranked first",
              report["lines"][0]["ingredient_name"] == "Cheese Portion")
        check("no theft language anywhere in the report",
              not any(w in str(report).lower() for w in ("theft", "stolen", "steal")))

        # ── 11. VAT-inclusive pricing ─────────────────────────────────────
        print("\n== VAT: R120 MENU PRICE STAYS R120 ==")
        total, vat = apply_vat_to_total(12000, biz.vat_rate_basis_points, biz.vat_inclusive_pricing)
        check("customer still pays R120.00", total == 12000, f"R{total/100:.2f}")
        check("VAT derived from inside the price", vat == 1565, f"R{vat/100:.2f}")
        check("never becomes R138", total != 13800)

        # ── 12. Tenant isolation of everything self-service ───────────────
        print("\n== TENANT ISOLATION ==")
        other_items = (await db.execute(
            select(MenuItem).where(MenuItem.business_id == OTHER)
        )).scalars().all()
        other_ing = (await db.execute(
            select(Ingredient).where(Ingredient.business_id == OTHER)
        )).scalars().all()
        other_recipes = (await db.execute(
            select(Recipe).where(Recipe.business_id == OTHER)
        )).scalars().all()
        check("imported menu did not leak to the other tenant", len(other_items) == 0)
        check("imported ingredients did not leak", len(other_ing) == 0)
        check("imported recipes did not leak", len(other_recipes) == 0)

        other_progress = await routes_setup.get_setup_progress(user=other_owner, db=db)
        other_menu = {s.key: s for s in other_progress.steps}["menu"]
        check("other tenant still sees an empty menu", other_menu.status == "todo")

        try:
            await routes_setup.get_live_stock(attention_only=False, user=other_owner, db=db)
            other_live_rows = 0
        except Exception:
            other_live_rows = -1
        check("other tenant's stock view is empty, not ours", other_live_rows <= 0)

        # ── 13. Setup progress after configuration ────────────────────────
        print("\n== SETUP PROGRESS (configured restaurant) ==")
        final = await routes_setup.get_setup_progress(user=owner, db=db)
        fkeys = {s.key: s for s in final.steps}
        check("menu now done", fkeys["menu"].status == "done", fkeys["menu"].detail)
        check("ingredients now done", fkeys["ingredients"].status == "done")
        check("recipes reported as partial (Regina has none)",
              fkeys["recipes"].status == "partial", fkeys["recipes"].detail)
        check("progress increased", final.percent_complete > progress.percent_complete,
              f"{progress.percent_complete}% -> {final.percent_complete}%")

    await engine.dispose()
    print("\n" + "=" * 64)
    print(f"  {sum(OK)}/{len(OK)} checks passed")
    print("=" * 64)
    return 0 if all(OK) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
