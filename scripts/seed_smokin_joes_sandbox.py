"""Smokin Joes - Cowey Road WhatsApp-simulator sandbox seed (LOCAL ONLY, never run against a real database).

Creates an ISOLATED copy of the Cowey Road demo tenant inside the disposable marketing/demo
sandbox database (port 55811) -- completely separate from the real production "Smokin Joes --
Cowey Road" business the user created via the Super Admin dashboard. Nothing here touches
production, Mochachos, or any real tenant. Menu prices/names are taken verbatim from
C:\\Users\\junsm\\Downloads\\smokin_joes_uber_menu_demo_2026-10-29.csv (21 Cowey Road rows,
sourced from recent public Uber Eats listings -- same provisional-demo caveat as the production
import). Ingredients/recipes/loyalty are the same fictional DEMO data already prepared for the
production tenant.

    cd <sandbox>/backend && DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55811/nextgen \
        MKT_PASSWORD=<pick-any-local-only-value> python -m scripts.seed_smokin_joes_sandbox
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
assert "55811" in os.environ.get("DATABASE_URL", ""), "refusing to run outside the sandbox"

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from shared.models import Business, LoyaltyProgram, LoyaltyReward  # noqa: E402
from shared.models.recipe import Recipe, RecipeItem  # noqa: E402
from backend.app.inventory.ledger import build_movement  # noqa: E402
from backend.app.inventory.units import to_milli  # noqa: E402
from shared.enums import StockMovementType  # noqa: E402
from scripts.seed_marketing import PASSWORD, ingredient, make_business, menu, money, users  # noqa: E402

PHONE_ID = "100000000000001"

# Verbatim from the 21 Cowey Road rows of the supplied CSV -- descriptions left blank,
# none were supplied and none are invented.
COWEY_ROAD_MENU = [
    ("New Burger", [
        ("The Karate Kid", 130, None),
        ("The Troublemaker", 175, None),
        ("The Pac Man", 174, None),
    ]),
    ("Road House Style", [
        ("Smoking Joe Burger", 164, None),
        ("Lightweight", 105, None),
        ("The Canelo", 158, None),
        ("Royale with Cheese Burger", 159, None),
        ("Nacho Libre Chicken", 153, None),
        ("The Champion Burger", 190, None),
        ("Nacho Libre Beef", 150, None),
        ("The Gypsy King Burger", 200, None),
        ("The Rassie Burger", 214, None),
    ]),
    ("Heavyweights", [
        ("The Raging Bull Burger", 224, None),
        ("Golden Boy", 235, None),
    ]),
    ("Sides", [
        ("Korean Fried Chicken Wings", 113, None),
        ("Big Poppa 8", 154, None),
        ("Plain Fries", 46, None),
        ("Thyme and Truffle Fries", 65, None),
    ]),
    ("Drinks and Shakes", [
        ("Chocolate", 94, None),
        ("Nutella", 105, None),
        ("Vietnamese Coffee", 99, None),
    ]),
]

# (name, canonical_unit, pack_qty, pack_unit, pack_cost_rand, count_method, par=(qty,unit), label)
INGREDIENTS = [
    ("DEMO Beef Patty 65g", "UNIT", 40, "unit", 280.00, "UNIT_COUNT", (20, "unit"), "box of 40"),
    ("DEMO Beef Patty 180g", "UNIT", 20, "unit", 320.00, "UNIT_COUNT", (10, "unit"), "box of 20"),
    ("DEMO Wagyu Patty", "UNIT", 20, "unit", 440.00, "UNIT_COUNT", (6, "unit"), "box of 20"),
    ("DEMO Chicken Wings (raw)", "GRAM", 10, "kg", 650.00, "WEIGHT", (3000, "g"), "10 kg case"),
    ("DEMO Burger Bun", "UNIT", 50, "unit", 175.00, "UNIT_COUNT", (30, "unit"), "tray of 50"),
    ("DEMO Cheddar Cheese Slice", "UNIT", 100, "unit", 210.00, "UNIT_COUNT", (60, "unit"), "pack of 100"),
    ("DEMO Burger Sauce", "MILLILITRE", 2000, "ml", 95.00, "VOLUME", (1000, "ml"), "2 L tub"),
    ("DEMO Jalapenos", "GRAM", 1000, "g", 60.00, "WEIGHT", (800, "g"), "1 kg tub"),
    ("DEMO Potatoes (fries)", "GRAM", 20, "kg", 240.00, "WEIGHT", (16000, "g"), "20 kg pocket"),
    ("DEMO Milkshake Mix - Chocolate", "MILLILITRE", 5000, "ml", 180.00, "VOLUME", (2000, "ml"), "5 L tub"),
    ("DEMO Milkshake Mix - Vanilla", "MILLILITRE", 5000, "ml", 175.00, "VOLUME", (2000, "ml"), "5 L tub"),
    ("DEMO Peach Ice Tea Stock", "MILLILITRE", 5000, "ml", 90.00, "VOLUME", (3000, "ml"), "5 L tub"),
    ("DEMO Lemon Ice Tea Stock", "MILLILITRE", 5000, "ml", 90.00, "VOLUME", (3000, "ml"), "5 L tub"),
    ("DEMO Vietnamese Coffee Concentrate", "MILLILITRE", 2000, "ml", 150.00, "VOLUME", (800, "ml"), "2 L tub"),
]

# Only lines whose menu_item is actually on the Cowey Road menu above.
RECIPES = [
    ("Smoking Joe Burger", [
        ("DEMO Beef Patty 65g", 1, "unit"),
        ("DEMO Burger Bun", 1, "unit"),
        ("DEMO Cheddar Cheese Slice", 1, "unit"),
        ("DEMO Burger Sauce", 30, "ml"),
    ]),
    ("The Gypsy King Burger", [
        ("DEMO Beef Patty 180g", 1, "unit"),
        ("DEMO Burger Bun", 1, "unit"),
        ("DEMO Cheddar Cheese Slice", 2, "unit"),
    ]),
    ("Korean Fried Chicken Wings", [("DEMO Chicken Wings (raw)", 300, "g")]),
    ("Plain Fries", [("DEMO Potatoes (fries)", 250, "g")]),
    ("Thyme and Truffle Fries", [("DEMO Potatoes (fries)", 250, "g")]),
    ("Chocolate", [("DEMO Milkshake Mix - Chocolate", 350, "ml")]),
    ("Vietnamese Coffee", [("DEMO Vietnamese Coffee Concentrate", 60, "ml")]),
]

REWARDS = [
    ("DEMO -- Free Regular Fries", 150, 46.00),
    ("DEMO -- Free Milkshake", 350, 94.00),
    ("DEMO -- R50 Off Order", 250, 50.00),
    ("DEMO -- R100 Off Order", 500, 100.00),
]


async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    S = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with S() as db:
        if (await db.execute(
            select(Business).where(Business.slug == "smokin-joes-cowey-road-sandbox")
        )).scalar_one_or_none():
            print("Smokin Joes Cowey Road sandbox already exists; nothing to do")
            return

        b = await make_business(
            db, name="Smokin Joes - Cowey Road (Sandbox)",
            slug="smokin-joes-cowey-road-sandbox", brand=("#2a1810", "#d66028"),
        )
        b.address = "54 Problem Mkhize Rd, Essenwood, Berea, Durban (demo data)"
        b.whatsapp_phone_number_id = PHONE_ID
        print(f"Business created: id={b.id} slug={b.slug} phone_id={PHONE_ID}")

        staff = await users(
            db, b, "owner@smokinjoescoweyroad-sandbox.example",
            ["Demo Owner", ("MANAGER", "Demo Manager"), ("STAFF", "Demo Staff")],
        )

        items = await menu(db, b, COWEY_ROAD_MENU)
        print(f"Menu imported: {len(items)} items across {len(COWEY_ROAD_MENU)} categories")

        db.add(LoyaltyProgram(
            business_id=b.id, is_enabled=True, points_per_rand=1, points_per_order=10,
            points_expiry_days=None, min_redemption_points=50,
            silver_tier_points=500, gold_tier_points=2000,
        ))
        for name, points_cost, value_rand in REWARDS:
            db.add(LoyaltyReward(
                business_id=b.id, name=name, description="Sandbox demo reward",
                points_cost=points_cost, is_active=True, value_cents=money(value_rand),
            ))
        print(f"Loyalty configured: {len(REWARDS)} rewards")

        await db.flush()

        I = {}
        for name, unit, pack_qty, pack_unit, pack_cost, method, par, label in INGREDIENTS:
            ing = ingredient(b, name, unit, pack_qty, pack_unit, pack_cost, method, par, label)
            db.add(ing)
            I[name] = ing
        await db.flush()
        print(f"Ingredients imported: {len(INGREDIENTS)}")

        recipe_lines = 0
        for item_name, lines in RECIPES:
            if item_name not in items:
                print(f"  SKIP recipe for {item_name!r} -- not on this menu")
                continue
            r = Recipe(business_id=b.id, menu_item_id=items[item_name].id, yield_qty=1)
            db.add(r)
            await db.flush()
            for ing_name, qty, unit in lines:
                db.add(RecipeItem(
                    business_id=b.id, recipe_id=r.id, ingredient_id=I[ing_name].id,
                    quantity_milli=to_milli(qty, unit),
                ))
                recipe_lines += 1
        print(f"Recipes imported: {len([r for r, _ in [(n, l) for n, l in RECIPES if n in items]])} items, {recipe_lines} ingredient lines")

        for ing in I.values():
            par = ing.par_level_milli or 0
            db.add(build_movement(
                business_id=b.id, ingredient_id=ing.id, quantity_milli=max(par * 2, 1000),
                movement_type=StockMovementType.OPENING_BALANCE.value,
                unit_cost_micro_cents=ing.cost_per_canonical_micro_cents,
            ))

        await db.commit()
        print(f"\nDONE. Business ID: {b.id}")
        print("Owner login: owner@smokinjoescoweyroad-sandbox.example / <value of MKT_PASSWORD you set> (local sandbox only)")
        print(f"Staff PIN login -- business code: {b.business_code}")


if __name__ == "__main__":
    asyncio.run(run())
