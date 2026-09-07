"""
End-to-end verification of the Ciao Pizza stock chain against a REAL Postgres.

    POS sale -> recipe resolution -> stock ledger -> wastage -> goods received
    -> stocktake -> variance

Runs against the throwaway container only. Asserts loudly; prints a summary.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://nextgen:testpw@localhost:55444/nextgen_test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_ADMIN_SECRET_KEY", "test-admin-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "t")
os.environ.setdefault("WHATSAPP_DEFAULT_ACCESS_TOKEN", "t")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from backend.app.inventory.ledger import (  # noqa: E402
    build_movement,
    movement_totals_by_type,
    post_sale_consumption,
    reverse_sale_consumption,
    stock_on_hand,
)
from backend.app.inventory.units import to_milli, cost_per_canonical_micro_cents  # noqa: E402
from backend.app.inventory.variance import IngredientVariance, basis_points  # noqa: E402
from backend.app.pos.pricing import ResolvedPOSItem  # noqa: E402
from shared.enums import StockMovementType, StockSourceType  # noqa: E402
from shared.models.business import Business  # noqa: E402
from shared.models.inventory import Ingredient  # noqa: E402
from shared.models.menu import MenuCategory, MenuItem  # noqa: E402
from shared.models.recipe import Recipe, RecipeItem  # noqa: E402
from shared.pricing.engine import LineItemBreakdown, apply_vat_to_total  # noqa: E402

OK = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    OK.append(bool(condition))
    print(f"  [{status}] {label}" + (f"  ({detail})" if detail else ""))


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # ── Two tenants, to prove isolation ──────────────────────────────
        ciao = Business(
            name="Ciao Pizza Lifestyle Centre", slug=f"ciao-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(),
            inventory_enabled=True, is_whatsapp_enabled=False,
            vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=True,
            vat_number="4123456789", receipt_paper_width_mm=80,
        )
        other = Business(
            name="Other Restaurant", slug=f"other-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(),
            inventory_enabled=True, is_whatsapp_enabled=True,
        )
        db.add_all([ciao, other]); await db.flush()

        print("\n== TENANT SETUP ==")
        check("Ciao created with inventory on, WhatsApp OFF",
              ciao.inventory_enabled and not ciao.is_whatsapp_enabled)
        check("Other tenant keeps WhatsApp ON", other.is_whatsapp_enabled)

        # ── Ingredients ──────────────────────────────────────────────────
        def ing(biz, name, unit, pack_qty, pack_unit, pack_cost, method="WEIGHT"):
            pack = to_milli(pack_qty, pack_unit)
            return Ingredient(
                business_id=biz.id, name=name, canonical_unit=unit, count_method=method,
                purchase_pack_milli=pack, cost_per_pack_cents=pack_cost,
                cost_per_canonical_micro_cents=cost_per_canonical_micro_cents(pack_cost, pack),
            )

        dough = ing(ciao, "Dough Ball", "UNIT", 1, "unit", 450, "UNIT_COUNT")
        cheese = ing(ciao, "Cheese", "GRAM", 2, "kg", 19000)
        sauce = ing(ciao, "Pizza Sauce", "GRAM", 1, "kg", 3800)
        chicken = ing(ciao, "Chicken", "GRAM", 1, "kg", 12000)
        mushroom = ing(ciao, "Mushroom", "GRAM", 500, "g", 4000)
        box = ing(ciao, "Large Pizza Box", "UNIT", 1, "unit", 350, "UNIT_COUNT")
        other_cheese = ing(other, "Cheese", "GRAM", 2, "kg", 19000)
        db.add_all([dough, cheese, sauce, chicken, mushroom, box, other_cheese])
        await db.flush()

        # Plain ids. A rollback expires ORM objects, and touching an expired
        # attribute afterwards triggers a lazy load in a sync context, which
        # asyncpg cannot serve (MissingGreenlet).
        CIAO, OTHER = ciao.id, other.id
        DOUGH, CHEESE, SAUCE = dough.id, cheese.id, sauce.id
        CHICKEN, MUSHROOM, BOX = chicken.id, mushroom.id, box.id
        OTHER_CHEESE = other_cheese.id
        CHEESE_COST = cheese.cost_per_canonical_micro_cents
        DOUGH_COST = dough.cost_per_canonical_micro_cents
        VAT_BP, VAT_INCL = ciao.vat_rate_basis_points, ciao.vat_inclusive_pricing
        PIZZA = None

        check("Cheese unit cost = 9.5c/g from R190/2kg",
              CHEESE_COST == 9_500_000,
              f"{CHEESE_COST}")

        # ── Menu + recipe (size-scoped) ──────────────────────────────────
        cat = MenuCategory(business_id=CIAO, name="Pizza")
        db.add(cat); await db.flush()
        SMALL, LARGE, GROUP, REM_MUSH = "opt-s", "opt-l", "grp-size", "rem-mush"
        pizza = MenuItem(
            business_id=CIAO, category_id=cat.id, name="Chicken Pizza",
            price_cents=12900, requires_preparation=True,
            options_json={
                "option_groups": [{
                    "id": GROUP, "name": "Size", "required": True, "is_enabled": True,
                    "options": [
                        {"id": SMALL, "name": "Small", "price_delta_cents": 0, "is_enabled": True},
                        {"id": LARGE, "name": "Large", "price_delta_cents": 3000, "is_enabled": True},
                    ],
                }],
                "removable_ingredients": [{"id": REM_MUSH, "name": "Mushroom"}],
            },
        )
        db.add(pizza); await db.flush()
        PIZZA = pizza.id

        recipe = Recipe(business_id=CIAO, menu_item_id=PIZZA, yield_qty=1)
        db.add(recipe); await db.flush()
        RECIPE = recipe.id
        db.add_all([
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=DOUGH,
                       quantity_milli=to_milli(1, "unit")),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=SAUCE,
                       quantity_milli=to_milli(80, "g")),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=CHEESE,
                       quantity_milli=to_milli(100, "g"), option_id=SMALL),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=CHEESE,
                       quantity_milli=to_milli(150, "g"), option_id=LARGE),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=CHICKEN,
                       quantity_milli=to_milli(70, "g")),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=MUSHROOM,
                       quantity_milli=to_milli(30, "g"), removable_ref_id=REM_MUSH),
            RecipeItem(business_id=CIAO, recipe_id=RECIPE, ingredient_id=BOX,
                       quantity_milli=to_milli(1, "unit")),
        ])
        await db.commit()

        # ── Opening stock ────────────────────────────────────────────────
        t0 = datetime.now(timezone.utc) - timedelta(hours=6)
        opening = {dough: (100, "unit"), cheese: (10_000, "g"), sauce: (4_000, "g"),
                   chicken: (3_000, "g"), mushroom: (1_000, "g"), box: (200, "unit")}
        for i, (qty, unit) in opening.items():
            db.add(build_movement(
                business_id=CIAO, ingredient_id=i.id, quantity_milli=to_milli(qty, unit),
                movement_type=StockMovementType.OPENING_BALANCE.value,
                unit_cost_micro_cents=i.cost_per_canonical_micro_cents, occurred_at=t0))
        db.add(build_movement(
            business_id=OTHER, ingredient_id=OTHER_CHEESE, quantity_milli=to_milli(5_000, "g"),
            movement_type=StockMovementType.OPENING_BALANCE.value, occurred_at=t0))
        await db.commit()

        levels = await stock_on_hand(db, CIAO)
        print("\n== OPENING STOCK ==")
        check("100 dough balls", levels[DOUGH] == to_milli(100, "unit"))
        check("10 000 g cheese", levels[CHEESE] == to_milli(10_000, "g"))

        # ── 40 large pizzas sold ─────────────────────────────────────────
        def resolved(qty, option_id, removed=()):
            return ResolvedPOSItem(
                menu_item_id=PIZZA, name="Chicken Pizza",
                breakdown=LineItemBreakdown(12900, 3000, 0, 15900, qty, 15900 * qty),
                selected_options_snapshot=[{"group_id": GROUP, "group_name": "Size",
                                           "option_id": option_id, "option_name": "Large",
                                           "price_delta_cents": 3000}],
                add_ons_snapshot=[],
                removed_ingredients_snapshot=[{"id": r, "name": "Mushroom"} for r in removed],
                special_instructions=None, requires_preparation=True,
            )

        t1 = datetime.now(timezone.utc) - timedelta(hours=3)
        order_id = uuid.uuid4()
        result = await post_sale_consumption(
            db, business_id=CIAO, order_id=order_id,
            resolved_items=[resolved(40, LARGE)], occurred_at=t1)
        await db.commit()

        print("\n== POS SALE -> RECIPE -> LEDGER (40 large pizzas) ==")
        cons = result.as_map()
        check("cheese 40 x 150 g = 6 000 g", cons[CHEESE] == to_milli(6_000, "g"))
        check("sauce 40 x 80 g = 3 200 g", cons[SAUCE] == to_milli(3_200, "g"))
        check("dough 40 units", cons[DOUGH] == to_milli(40, "unit"))
        check("no missing recipes", not result.menu_items_without_recipe)

        # ── Idempotency ──────────────────────────────────────────────────
        try:
            await post_sale_consumption(db, business_id=CIAO, order_id=order_id,
                                        resolved_items=[resolved(40, LARGE)], occurred_at=t1)
            await db.commit()
            dup = False
        except Exception:
            await db.rollback()
            dup = True
        after = await stock_on_hand(db, CIAO)
        print("\n== IDEMPOTENCY ==")
        check("re-posting the same order is rejected by the unique index", dup)
        check("cheese deducted exactly once",
              after[CHEESE] == to_milli(10_000 - 6_000, "g"),
              f"{after[CHEESE]}")

        # ── Removal ──────────────────────────────────────────────────────
        rem_order = uuid.uuid4()
        rem = await post_sale_consumption(db, business_id=CIAO, order_id=rem_order,
                                          resolved_items=[resolved(1, LARGE, removed=(REM_MUSH,))],
                                          occurred_at=t1)
        await db.commit()
        print("\n== REMOVAL ==")
        check("'no mushroom' deducts zero mushroom", MUSHROOM not in rem.as_map())
        check("cheese still deducted on that pizza", rem.as_map()[CHEESE] == to_milli(150, "g"))

        # ── Void reverses ────────────────────────────────────────────────
        n = await reverse_sale_consumption(db, business_id=CIAO, order_id=rem_order,
                                           notes="void test")
        await db.commit()
        again = await reverse_sale_consumption(db, business_id=CIAO, order_id=rem_order)
        await db.commit()
        lvl = await stock_on_hand(db, CIAO)
        print("\n== VOID REVERSAL ==")
        check("reversal wrote movements", n > 0, f"{n} lines")
        check("second void writes nothing (idempotent)", again == 0)
        check("cheese back to post-40-sale level",
              lvl[CHEESE] == to_milli(4_000, "g"), f"{lvl[CHEESE]}")

        # ── Recipe change must not rewrite history ───────────────────────
        for line in (await db.get(Recipe, RECIPE)).items:
            if line.ingredient_id == CHEESE and line.option_id == LARGE:
                line.quantity_milli = to_milli(140, "g")
        await db.commit()
        totals = await movement_totals_by_type(db, CIAO, ingredient_ids=[CHEESE])
        consumed = -totals.get((CHEESE, StockMovementType.SALE_CONSUMPTION.value), 0)
        print("\n== HISTORICAL ACCURACY ==")
        check("recipe changed 150 g -> 140 g today",
              any(l.quantity_milli == to_milli(140, "g")
                  for l in (await db.get(Recipe, RECIPE)).items))
        check("yesterday's ledger still says 150 g/pizza",
              consumed == to_milli(6_000 + 150, "g"), f"{consumed} milli-g")

        # ── Goods received + wastage ─────────────────────────────────────
        t2 = datetime.now(timezone.utc) - timedelta(hours=2)
        db.add(build_movement(business_id=CIAO, ingredient_id=CHEESE,
                              quantity_milli=to_milli(5_000, "g"),
                              movement_type=StockMovementType.GOODS_RECEIVED.value,
                              unit_cost_micro_cents=CHEESE_COST,
                              occurred_at=t2))
        db.add(build_movement(business_id=CIAO, ingredient_id=CHEESE,
                              quantity_milli=-to_milli(250, "g"),
                              movement_type=StockMovementType.WASTAGE.value,
                              unit_cost_micro_cents=CHEESE_COST,
                              occurred_at=t2))
        db.add(build_movement(business_id=CIAO, ingredient_id=DOUGH,
                              quantity_milli=-to_milli(2, "unit"),
                              movement_type=StockMovementType.WASTAGE.value,
                              unit_cost_micro_cents=DOUGH_COST,
                              occurred_at=t2))
        await db.commit()

        # ── Variance, the client's scenario 2 ────────────────────────────
        cutoff = datetime.now(timezone.utc)
        tot = await movement_totals_by_type(db, CIAO, until=cutoff, ingredient_ids=[CHEESE])
        received = tot.get((CHEESE, StockMovementType.GOODS_RECEIVED.value), 0)
        consumed = -(tot.get((CHEESE, StockMovementType.SALE_CONSUMPTION.value), 0)
                     + tot.get((CHEESE, StockMovementType.SALE_REVERSAL.value), 0))
        wasted = -tot.get((CHEESE, StockMovementType.WASTAGE.value), 0)
        opening_c = tot.get((CHEESE, StockMovementType.OPENING_BALANCE.value), 0)

        v = IngredientVariance(
            opening_milli=opening_c, received_milli=received,
            theoretical_consumption_milli=consumed, wastage_milli=wasted,
            counted_milli=to_milli(8_600, "g"),
            unit_cost_micro_cents=CHEESE_COST)

        print("\n== VARIANCE (client scenario 2: cheese) ==")
        check("opening 10 000 g", opening_c == to_milli(10_000, "g"))
        check("received 5 000 g", received == to_milli(5_000, "g"))
        check("theoretical consumption 6 000 g (void netted out)",
              consumed == to_milli(6_000, "g"), f"{consumed}")
        check("wastage 250 g", wasted == to_milli(250, "g"))
        check("expected closing 8 750 g", v.expected_milli == to_milli(8_750, "g"),
              f"{v.expected_milli}")
        check("variance -150 g", v.variance_milli == to_milli(-150, "g"))
        check("variance value -R14.25", v.variance_value_cents == -1425)
        check("variance -2.40% of usage",
              basis_points(v.variance_milli, to_milli(6_250, "g")) == -240)

        # ── VAT ──────────────────────────────────────────────────────────
        total, vat = apply_vat_to_total(11500, VAT_BP, VAT_INCL)
        print("\n== VAT ==")
        check("R115 inclusive contains R15 VAT, total unchanged", total == 11500 and vat == 1500)

        # ── Tenant isolation ─────────────────────────────────────────────
        ciao_levels = await stock_on_hand(db, CIAO)
        other_levels = await stock_on_hand(db, OTHER)
        print("\n== TENANT ISOLATION ==")
        check("Ciao's ledger excludes the other tenant's ingredient",
              OTHER_CHEESE not in ciao_levels)
        check("Other tenant untouched by 40 pizzas sold at Ciao",
              other_levels[OTHER_CHEESE] == to_milli(5_000, "g"))
        check("Other tenant sees only its own ingredient", len(other_levels) == 1)

    await engine.dispose()

    print("\n" + "=" * 62)
    print(f"  {sum(OK)}/{len(OK)} checks passed")
    print("=" * 62)
    return 0 if all(OK) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
