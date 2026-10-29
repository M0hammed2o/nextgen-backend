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
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
assert "55811" in os.environ.get("DATABASE_URL", ""), "refusing to run outside the sandbox"

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from shared.models import Business, Customer, LoyaltyProgram, LoyaltyReward  # noqa: E402
from shared.models.recipe import Recipe, RecipeItem  # noqa: E402
from backend.app.inventory.ledger import build_movement  # noqa: E402
from backend.app.inventory.units import to_milli  # noqa: E402
from shared.enums import StockMovementType  # noqa: E402
from scripts import seed_marketing  # noqa: E402
from scripts.seed_marketing import PASSWORD, history, ingredient, make_business, menu, money, users  # noqa: E402

PHONE_ID = "100000000000001"

# Removable-ingredient option groups, only on the two burgers that have a
# recipe below -- real customisations, not invented menu items. "No
# Jalapenos" needed a jalapeno line added to Smoking Joe Burger's recipe
# (see RECIPES below) since the base CSV import has none; the rest
# (sauce/cheese) already exist on at least one of these two recipes.
SMOKING_JOE_OPTS = {"removable_ingredients": [
    {"id": "rm-sauce", "name": "Sauce"}, {"id": "rm-cheese", "name": "Cheese"}, {"id": "rm-jalapenos", "name": "Jalapenos"},
]}
GYPSY_KING_OPTS = {"removable_ingredients": [{"id": "rm-cheese", "name": "Cheese"}]}

# Provisional DEMO prices -- not confirmed Smokin Joes pricing, same caveat
# as the rest of this menu. Targets both burgers that have a recipe.
ADD_ON_SPEC = [("Extra Cheese", 12.00), ("Extra Beef Patty", 35.00)]
ADD_ON_TARGETS = ("Smoking Joe Burger", "The Gypsy King Burger")

# Verbatim from the 21 Cowey Road rows of the supplied CSV -- descriptions left blank,
# none were supplied and none are invented.
COWEY_ROAD_MENU = [
    ("New Burger", [
        ("The Karate Kid", 130, None),
        ("The Troublemaker", 175, None),
        ("The Pac Man", 174, None),
    ]),
    ("Road House Style", [
        ("Smoking Joe Burger", 164, None, SMOKING_JOE_OPTS),
        ("Lightweight", 105, None),
        ("The Canelo", 158, None),
        ("Royale with Cheese Burger", 159, None),
        ("Nacho Libre Chicken", 153, None),
        ("The Champion Burger", 190, None),
        ("Nacho Libre Beef", 150, None),
        ("The Gypsy King Burger", 200, None, GYPSY_KING_OPTS),
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
# 4th tuple element (optional) = removable_ref_id, matching the option ids
# in SMOKING_JOE_OPTS/GYPSY_KING_OPTS above -- these lines are skipped at
# sale time when the customer removes that ingredient (POS only -- the
# WhatsApp bot pipeline has no structured removal concept, see
# order_creator.py's own docstring; a WhatsApp "no cheese" stays free text
# and does NOT reduce consumption, which is a real, documented platform
# limitation, not something this seed can configure around).
RECIPES = [
    ("Smoking Joe Burger", [
        ("DEMO Beef Patty 65g", 1, "unit", None),
        ("DEMO Burger Bun", 1, "unit", None),
        ("DEMO Cheddar Cheese Slice", 1, "unit", "rm-cheese"),
        ("DEMO Burger Sauce", 30, "ml", "rm-sauce"),
        ("DEMO Jalapenos", 10, "g", "rm-jalapenos"),
    ]),
    ("The Gypsy King Burger", [
        ("DEMO Beef Patty 180g", 1, "unit", None),
        ("DEMO Burger Bun", 1, "unit", None),
        ("DEMO Cheddar Cheese Slice", 2, "unit", "rm-cheese"),
    ]),
    ("Korean Fried Chicken Wings", [("DEMO Chicken Wings (raw)", 300, "g", None)]),
    ("Plain Fries", [("DEMO Potatoes (fries)", 250, "g", None)]),
    ("Thyme and Truffle Fries", [("DEMO Potatoes (fries)", 250, "g", None)]),
    ("Chocolate", [("DEMO Milkshake Mix - Chocolate", 350, "ml", None)]),
    ("Vietnamese Coffee", [("DEMO Vietnamese Coffee Concentrate", 60, "ml", None)]),
]

# Add-on recipes (Recipe.menu_add_on_id, not menu_item_id) -- so selecting
# "Extra Cheese"/"Extra Beef Patty" at the till actually deducts extra
# stock, same real mechanism as a menu item's own recipe.
ADD_ON_RECIPES = {
    "Extra Cheese": [("DEMO Cheddar Cheese Slice", 1, "unit")],
    "Extra Beef Patty": [("DEMO Beef Patty 65g", 1, "unit")],
}

REWARDS = [
    ("DEMO -- Free Regular Fries", 150, 46.00),
    ("DEMO -- Free Milkshake", 350, 94.00),
    ("DEMO -- R50 Off Order", 250, 50.00),
    ("DEMO -- R100 Off Order", 500, 100.00),
]

# Popularity weights for history() -- every item that can realistically sell;
# burgers and the better-known sides weighted higher, matching ordinary
# restaurant demand shape. Fictional, for demo realism only.
SMOKIN_JOES_POP = {
    "The Karate Kid": 8, "The Troublemaker": 9, "The Pac Man": 10,
    "Smoking Joe Burger": 16, "Lightweight": 7, "The Canelo": 9, "Royale with Cheese Burger": 11,
    "Nacho Libre Chicken": 8, "The Champion Burger": 10, "Nacho Libre Beef": 7,
    "The Gypsy King Burger": 15, "The Rassie Burger": 9,
    "The Raging Bull Burger": 6, "Golden Boy": 5,
    "Korean Fried Chicken Wings": 14, "Big Poppa 8": 7, "Plain Fries": 18, "Thyme and Truffle Fries": 11,
    "Chocolate": 9, "Nutella": 7, "Vietnamese Coffee": 6,
}

# Opening stock multiple over par_level, PER INGREDIENT -- sized from a real
# dry-run's measured 184-day consumption per ingredient (not guessed), so
# none of them actually go negative. Three ingredients (cheese/fries/buns)
# deliberately given only enough headroom to land just above zero by
# "today" -- a DERIVED low-stock situation from real transactions, not a
# hardcoded dashboard number. Ingredients with zero historical consumption
# (not in SMOKIN_JOES_POP/RECIPES) get a flat, generous default since
# there's no real consumption rate to size against.
_UNUSED_DEFAULT_MULTIPLE = 45
OPENING_STOCK_MULTIPLE = {
    "DEMO Beef Patty 180g": 65,
    "DEMO Beef Patty 65g": 33,
    "DEMO Burger Bun": 33,          # LOW -- near par by "today"
    "DEMO Burger Sauce": 19,
    "DEMO Cheddar Cheese Slice": 22,  # LOW -- near par by "today"
    "DEMO Chicken Wings (raw)": 62,
    "DEMO Jalapenos": 8,
    "DEMO Milkshake Mix - Chocolate": 60,
    "DEMO Potatoes (fries)": 15,    # LOW -- near par by "today"
    "DEMO Vietnamese Coffee Concentrate": 20,
}


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

        items = await menu(db, b, COWEY_ROAD_MENU, ADD_ON_SPEC, ADD_ON_TARGETS)
        print(f"Menu imported: {len(items)} items across {len(COWEY_ROAD_MENU)} categories")

        from shared.models import MenuAddOn
        add_ons = {
            ao.name: ao
            for ao in (
                await db.execute(select(MenuAddOn).where(MenuAddOn.business_id == b.id))
            ).scalars().all()
        }
        print(f"Add-ons configured: {list(add_ons.keys())}, targeting {ADD_ON_TARGETS}")

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
            for ing_name, qty, unit, removable_ref_id in lines:
                db.add(RecipeItem(
                    business_id=b.id, recipe_id=r.id, ingredient_id=I[ing_name].id,
                    quantity_milli=to_milli(qty, unit), removable_ref_id=removable_ref_id,
                ))
                recipe_lines += 1
        print(f"Recipes imported: {len([n for n, _ in RECIPES if n in items])} items, {recipe_lines} ingredient lines")

        add_on_recipe_lines = 0
        for add_on_name, lines in ADD_ON_RECIPES.items():
            if add_on_name not in add_ons:
                print(f"  SKIP add-on recipe for {add_on_name!r} -- not configured")
                continue
            r = Recipe(business_id=b.id, menu_add_on_id=add_ons[add_on_name].id, yield_qty=1)
            db.add(r)
            await db.flush()
            for ing_name, qty, unit in lines:
                db.add(RecipeItem(
                    business_id=b.id, recipe_id=r.id, ingredient_id=I[ing_name].id,
                    quantity_milli=to_milli(qty, unit),
                ))
                add_on_recipe_lines += 1
        print(f"Add-on recipes imported: {len(ADD_ON_RECIPES)} add-ons, {add_on_recipe_lines} ingredient lines")

        opening_at = seed_marketing.NOW - timedelta(days=185)
        for name, ing in I.items():
            par = ing.par_level_milli or 0
            multiple = OPENING_STOCK_MULTIPLE.get(name, _UNUSED_DEFAULT_MULTIPLE)
            db.add(build_movement(
                business_id=b.id, ingredient_id=ing.id, quantity_milli=max(int(par * multiple), 1000),
                movement_type=StockMovementType.OPENING_BALANCE.value,
                unit_cost_micro_cents=ing.cost_per_canonical_micro_cents, occurred_at=opening_at,
            ))
        await db.commit()
        print(f"Opening stock posted for {len(I)} ingredients ({opening_at.date()})")

        # ~6 fictional demonstration customers' worth of real purchasing-pattern
        # variety -- fictional names, reserved/undialable +27 00 test-range
        # numbers (never a real, messageable WhatsApp number).
        customers = []
        for i in range(60):
            nm = f"{seed_marketing.FIRST[i % len(seed_marketing.FIRST)]} {seed_marketing.LAST[(i * 7) % len(seed_marketing.LAST)]}"
            c = Customer(
                business_id=b.id, wa_id=f"2700001{2000 + i:04d}", display_name=nm, phone_number=f"+27000{2000 + i:04d}",
                loyalty_points_balance=0, loyalty_tier="BRONZE",
                last_message_at=seed_marketing.NOW - timedelta(days=seed_marketing.R.randint(0, 30)),
            )
            customers.append(c)
        db.add_all(customers)
        await db.flush()
        print(f"Demo customers created: {len(customers)}")

        # Reuse seed_marketing's proven history()/build_line() unchanged --
        # repoint its two small item-name config constants at Smokin Joes'
        # own burger/add-on names rather than duplicating that logic.
        seed_marketing.BURGERS = ("Smoking Joe Burger", "The Gypsy King Burger")
        seed_marketing.ADD_ONS = ADD_ON_SPEC
        last_seq = await history(
            db, b, staff, items, customers,
            days=183, base=12, pop=SMOKIN_JOES_POP, whatsapp_share=0.42,
        )
        print(f"Historical orders generated: 184 days (2026-05-ish through today), last order_number sequence={last_seq}")

        # history() doesn't call the loyalty engine itself (it's a sales/stock
        # generator, not a payment-confirmation call site) -- backfill points
        # using the EXACT SAME production function real payment confirmations
        # call (award_loyalty_points_if_enabled), once per paid order that has
        # a customer, so balances are genuinely derived from these orders,
        # not invented.
        from backend.app.loyalty.engine import award_loyalty_points_if_enabled
        from shared.models.order import Order
        paid_orders_with_customer = (
            await db.execute(
                select(Order.id, Order.customer_id, Order.total_cents).where(
                    Order.business_id == b.id, Order.payment_status == "PAID", Order.customer_id.is_not(None),
                )
            )
        ).all()
        awarded = 0
        for order_id, customer_id, total_cents in paid_orders_with_customer:
            order_stub = type("OrderStub", (), {"id": order_id, "customer_id": customer_id, "total_cents": total_cents})()
            await award_loyalty_points_if_enabled(db, b.id, order_stub)
            awarded += 1
        print(f"Loyalty points backfilled for {awarded} paid orders with a customer")

        print(f"\nDONE. Business ID: {b.id}")
        print("Owner login: owner@smokinjoescoweyroad-sandbox.example / <value of MKT_PASSWORD you set> (local sandbox only)")
        print(f"Staff PIN login -- business code: {b.business_code}")


if __name__ == "__main__":
    asyncio.run(run())
