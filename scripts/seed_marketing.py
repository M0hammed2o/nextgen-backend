"""Marketing-capture sandbox seed (LOCAL ONLY, never run against a real database).

Creates a fictional restaurant, "Saffron Street Kitchen", with a realistic menu, inventory + recipes, staff, customers,
loyalty and 35 days of order history (POS + WhatsApp), plus three small fictional Souk vendors with their own branding.
Stock movements for every historical sale go through the real ledger (post_sale_consumption), so stock levels,
variance and analytics are computed by the platform's own code. All names are fictional; phone numbers use the
undialable +27 00 prefix.

    python -m scripts.seed_marketing   (DATABASE_URL must point at the sandbox container)
"""
import asyncio
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
assert "55811" in os.environ.get("DATABASE_URL", ""), "refusing to run outside the marketing sandbox"

from sqlalchemy import update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from backend.app.core.security import hash_password, hash_pin  # noqa: E402
from backend.app.inventory.ledger import build_movement, post_sale_consumption  # noqa: E402
from backend.app.inventory.units import cost_per_canonical_micro_cents, to_milli  # noqa: E402
from backend.app.pos.pricing import ResolvedPOSItem  # noqa: E402
from shared.enums import StockMovementType  # noqa: E402
from shared.models import (Business, BusinessUser, Customer, LoyaltyProgram, LoyaltyReward, MenuAddOn,  # noqa: E402
                           MenuCategory, MenuItem, Special)
from shared.models.inventory import Ingredient, Supplier  # noqa: E402
from shared.models.menu import menu_item_add_ons  # noqa: E402
from shared.models.order import Order, OrderItem  # noqa: E402
from shared.models.recipe import Recipe, RecipeItem  # noqa: E402
from shared.models.till import TillSession  # noqa: E402
from shared.pricing.engine import LineItemBreakdown  # noqa: E402
from shared.utils import format_order_number, generate_business_code  # noqa: E402

R = random.Random(20261027)
NOW = datetime.now(timezone.utc)
SAST = timezone(timedelta(hours=2))
PASSWORD = os.environ["MKT_PASSWORD"]   # required, no hardcoded fallback -- local sandbox only, never commit a real value
OPEN_ALL_WEEK = {d: {"open": "00:00", "close": "23:59"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

FIRST = ["Aisha", "Thabo", "Lerato", "Yusuf", "Fatima", "Sipho", "Naledi", "Priya", "Ridhwaan", "Kayla", "Johan", "Ayesha",
         "Mbali", "Ahmed", "Chloe", "Musa", "Tasneem", "Imraan", "Nomsa", "Zaid", "Bianca", "Kagiso", "Hannah", "Rashaad",
         "Zanele", "Daniel", "Safiyyah", "Lwazi", "Megan", "Faheem", "Palesa", "Tariq", "Lindiwe", "Ethan", "Nadia", "Bongani",
         "Shaakira", "Liam", "Thandi", "Waseem", "Amara", "Kyle", "Refilwe", "Hamza", "Jessica", "Ntando", "Mariam", "Ruan"]
LAST = ["Patel", "Nkosi", "Dlamini", "Khan", "Essop", "Mahlangu", "Khumalo", "Naidoo", "Cassim", "van der Merwe", "Botha",
        "Ismail", "Zulu", "Seedat", "Smith", "Ndlovu", "Jeewa", "Desai", "Mokoena", "Hendricks", "Pillay", "Molefe", "Adams",
        "Bhayat", "Mthembu", "Jacobs", "Peer", "Sithole", "Williams", "Mayet", "Nel", "Govender"]


def money(r):
    return int(round(r * 100))


async def make_business(db, *, name, slug, brand, greeting=None, inventory=True, delivery=False):
    b = Business(
        name=name, slug=slug, business_code=generate_business_code(), timezone="Africa/Johannesburg", currency="ZAR",
        business_hours=OPEN_ALL_WEEK, greeting_text=greeting, address="Fictional demo restaurant", phone="+27000000000",
        delivery_enabled=delivery, delivery_fee_cents=0, payment_provider="MOCK", payment_methods_enabled=["PAYMENT_LINK"],
        max_staff_discount_cents=2000, billing_status="ACTIVE", plan="GROWTH",
        is_whatsapp_enabled=True, inventory_enabled=inventory, loyalty_enabled=True, pos_enabled=True,
        advanced_analytics_enabled=True, digital_receipts_enabled=False,
        vat_registered=True, vat_rate_basis_points=1500, vat_inclusive_pricing=True, vat_number="4000000000",
        require_customer_name=True, require_phone_number=False,
        staff_live_orders_tab_enabled=True, staff_wastage_tab_enabled=inventory, staff_stocktake_tab_enabled=inventory,
        pos_cash_enabled=True, pos_card_enabled=True, pos_other_payment_enabled=False,
        brand_primary_colour=brand[0], brand_accent_colour=brand[1], brand_display_name=name, brand_theme="LIGHT",
    )
    db.add(b)
    await db.flush()
    return b


async def users(db, b, owner_email, people):
    out = {}
    db.add(BusinessUser(business_id=b.id, role="OWNER", staff_name=people[0], email=owner_email,
                        password_hash=hash_password(PASSWORD), is_active=True, must_change_password=False))
    for i, (role, nm) in enumerate(people[1:]):
        u = BusinessUser(business_id=b.id, role=role, staff_name=nm, email=None, pin_hash=hash_pin(f"{2468 + i}"),
                         pin_updated_at=NOW, is_active=True, must_change_password=False)
        db.add(u)
        out[nm] = u
    await db.flush()
    return out


async def menu(db, b, spec, add_on_spec=None, add_on_targets=()):
    items = {}
    for ci, (cat_name, rows) in enumerate(spec):
        cat = MenuCategory(business_id=b.id, name=cat_name, sort_order=ci)
        db.add(cat)
        await db.flush()
        for ii, row in enumerate(rows):
            nm, price, desc = row[:3]
            opts = row[3] if len(row) > 3 else None
            it = MenuItem(business_id=b.id, category_id=cat.id, name=nm, price_cents=money(price), description=desc,
                          options_json=opts, sort_order=ii, requires_preparation=cat_name not in ("Drinks",))
            db.add(it)
            items[nm] = it
    await db.flush()
    for nm, price in (add_on_spec or []):
        ao = MenuAddOn(business_id=b.id, name=nm, price_cents=money(price), min_qty=0, max_qty=3, default_qty=1)
        db.add(ao)
        await db.flush()
        for t in add_on_targets:
            await db.execute(pg_insert(menu_item_add_ons).values(menu_item_id=items[t].id, add_on_id=ao.id).on_conflict_do_nothing())
    return items


def ingredient(b, name, unit, pack_qty, pack_unit, pack_cost, method, par=None, label=None, supplier=None):
    pack = to_milli(pack_qty, pack_unit)
    return Ingredient(business_id=b.id, name=name, canonical_unit=unit, count_method=method, purchase_pack_milli=pack,
                      cost_per_pack_cents=money(pack_cost), cost_per_canonical_micro_cents=cost_per_canonical_micro_cents(money(pack_cost), pack),
                      par_level_milli=to_milli(*par) if par else None, purchase_unit_name=label)


BURGER_OPTS = {"removable_ingredients": [{"id": "rm-onion", "name": "Onion"}, {"id": "rm-tomato", "name": "Tomato"},
                                         {"id": "rm-lettuce", "name": "Lettuce"}, {"id": "rm-pickles", "name": "Pickles"}]}
CHICKEN_BURGER_OPTS = {"option_groups": [{"id": "grp-style", "name": "Style", "required": True, "is_enabled": True, "options": [
    {"id": "opt-grilled", "name": "Grilled", "price_delta_cents": 0, "is_enabled": True},
    {"id": "opt-crispy", "name": "Crispy", "price_delta_cents": 0, "is_enabled": True}]}], **BURGER_OPTS}
SHAKE_OPTS = {"option_groups": [{"id": "grp-flavour", "name": "Flavour", "required": True, "is_enabled": True, "options": [
    {"id": "opt-choc", "name": "Chocolate", "price_delta_cents": 0, "is_enabled": True},
    {"id": "opt-van", "name": "Vanilla", "price_delta_cents": 0, "is_enabled": True},
    {"id": "opt-straw", "name": "Strawberry", "price_delta_cents": 0, "is_enabled": True}]}]}

MAIN_MENU = [
    ("Burgers", [("Classic Beef Burger", 89, "Flame-grilled beef patty, cheddar, lettuce, tomato, house sauce.", BURGER_OPTS),
                 ("Chicken Burger", 79, "Grilled or crispy chicken fillet, lettuce, mayo, toasted bun.", CHICKEN_BURGER_OPTS),
                 ("Double Smash Burger", 119, "Two smashed beef patties, double cheddar, pickles, onions.", BURGER_OPTS),
                 ("Saffron Spiced Burger", 99, "Spiced beef, caramelised onions, saffron aioli.", BURGER_OPTS)]),
    ("Chicken & Wraps", [("Quarter Chicken & Chips", 79, "Flame-grilled quarter chicken with regular chips."),
                         ("Chicken Wings (6)", 69, "Six wings, lemon-and-herb or hot."),
                         ("Chicken Shawarma Wrap", 75, "Shawarma chicken, garlic sauce, pickles, chips inside."),
                         ("Falafel Wrap", 65, "Crispy falafel, hummus, salad, tahini.")]),
    ("Sides", [("Regular Chips", 30, "Hand-cut, salted."), ("Large Chips", 42, "Hand-cut, salted."),
               ("Onion Rings", 35, "Beer-free batter, crispy."), ("Garden Salad", 45, "Greens, tomato, cucumber, feta.")]),
    ("Drinks", [("Coke 330ml", 22, None), ("Fresh Lemonade", 32, "Freshly squeezed, mint."), ("Iced Coffee", 38, None)]),
    ("Desserts", [("Milkshake", 45, "Thick shake, choose a flavour.", SHAKE_OPTS), ("Baklava Cheesecake", 55, "Baked cheesecake, pistachio, honey.")]),
]
ADD_ONS = [("Extra Cheese", 12), ("Jalapeños", 8), ("Fried Egg", 10), ("Extra Patty", 35), ("Caramelised Onions", 10)]
BURGERS = ("Classic Beef Burger", "Chicken Burger", "Double Smash Burger", "Saffron Spiced Burger")

# popularity weights (best-sellers): what customers order
POP = {"Chicken Burger": 18, "Classic Beef Burger": 15, "Large Chips": 14, "Regular Chips": 12, "Double Smash Burger": 9,
       "Chicken Shawarma Wrap": 9, "Coke 330ml": 13, "Quarter Chicken & Chips": 7, "Chicken Wings (6)": 6, "Milkshake": 6,
       "Saffron Spiced Burger": 5, "Falafel Wrap": 4, "Fresh Lemonade": 5, "Onion Rings": 4, "Iced Coffee": 3,
       "Baklava Cheesecake": 3, "Garden Salad": 2}


async def main_restaurant(db):
    b = await make_business(db, name="Saffron Street Kitchen", slug="saffron-street-kitchen", brand=("#202124", "#B8BDC3"),
                            delivery=True)
    b.whatsapp_phone_number_id = "100000000000001"
    b.whatsapp_display_phone_number = None if not hasattr(b, "whatsapp_display_phone_number") else "+27 00 000 0100"
    staff = await users(db, b, "owner@saffronstreet.example",
                        ["Yasmin Adams", ("MANAGER", "Yasmin (Manager)"), ("STAFF", "Sipho"), ("STAFF", "Lerato"), ("STAFF", "Imraan")])
    items = await menu(db, b, MAIN_MENU, ADD_ONS, BURGERS)
    db.add(Special(business_id=b.id, title="Tuesday Burger Deal", description="Any burger + regular chips for R99 on Tuesdays.",
                   days_of_week=["tue"], sort_order=0))
    db.add(LoyaltyProgram(business_id=b.id, is_enabled=True, points_per_rand=1, points_per_order=5, points_expiry_days=365,
                          min_redemption_points=100, silver_tier_points=500, gold_tier_points=2000))
    for nm, d, pts in (("Free Regular Chips", "Any order, once per visit.", 150), ("Free Milkshake", "Any flavour.", 300),
                       ("Free Chicken Burger", "Grilled or crispy.", 600)):
        db.add(LoyaltyReward(business_id=b.id, name=nm, description=d, points_cost=pts))

    sup = [Supplier(business_id=b.id, name=n, contact_name=c) for n, c in
           (("Coastal Meats (fictional)", "Accounts"), ("Fresh Fields Produce (fictional)", "Orders"), ("Metro Packaging (fictional)", "Sales"))]
    db.add_all(sup)
    await db.flush()
    M, P, K = sup[0].id, sup[1].id, sup[2].id
    I = {}
    for args in (
        ("Beef Patty 150g", "UNIT", 24, "unit", 432, "UNIT_COUNT", (120, "unit"), "box of 24", M),
        ("Chicken Fillet", "UNIT", 20, "unit", 380, "UNIT_COUNT", (100, "unit"), "box of 20", M),
        ("Chicken Quarter", "UNIT", 12, "unit", 216, "UNIT_COUNT", (48, "unit"), "pack of 12", M),
        ("Chicken Wings", "UNIT", 60, "unit", 330, "UNIT_COUNT", (180, "unit"), "box of 60", M),
        ("Shawarma Chicken", "GRAM", 5, "kg", 475, "WEIGHT", (8000, "g"), "5 kg tub", M),
        ("Burger Bun", "UNIT", 36, "unit", 162, "UNIT_COUNT", (180, "unit"), "tray of 36", P),
        ("Cheddar Slice", "UNIT", 84, "unit", 210, "UNIT_COUNT", (200, "unit"), "pack of 84", P),
        ("Potatoes", "GRAM", 10, "kg", 95, "WEIGHT", (40000, "g"), "10 kg pocket", P),
        ("Lettuce", "GRAM", 1, "kg", 38, "WEIGHT", (3000, "g"), "1 kg", P),
        ("Tomato", "GRAM", 1, "kg", 32, "WEIGHT", (3000, "g"), "1 kg", P),
        ("Onion", "GRAM", 10, "kg", 120, "WEIGHT", (8000, "g"), "10 kg bag", P),
        ("House Sauce", "MILLILITRE", 5, "litre", 210, "VOLUME", (4000, "ml"), "5 L", P),
        ("Tortilla Wrap", "UNIT", 48, "unit", 168, "UNIT_COUNT", (96, "unit"), "case of 48", P),
        ("Falafel", "UNIT", 60, "unit", 150, "UNIT_COUNT", (90, "unit"), "bag of 60", P),
        ("Milk", "MILLILITRE", 2, "litre", 36, "VOLUME", (12000, "ml"), "2 L", P),
        ("Ice Cream", "MILLILITRE", 5, "litre", 185, "VOLUME", (10000, "ml"), "5 L tub", P),
        ("Lemons", "UNIT", 30, "unit", 75, "UNIT_COUNT", (60, "unit"), "pocket of 30", P),
        ("Coke 330ml Can", "UNIT", 24, "unit", 228, "UNIT_COUNT", (96, "unit"), "tray of 24", K),
        ("Cooking Oil", "MILLILITRE", 20, "litre", 610, "VOLUME", (20000, "ml"), "20 L drum", K),
        ("Takeaway Box", "UNIT", 250, "unit", 375, "UNIT_COUNT", (300, "unit"), "sleeve of 250", K),
    ):
        ing = ingredient(b, *args)
        db.add(ing)
        I[args[0]] = ing
    await db.flush()

    def rec(item, lines):
        r = Recipe(business_id=b.id, menu_item_id=items[item].id, yield_qty=1)
        db.add(r)
        return r, lines
    recipes = [
        rec("Classic Beef Burger", [("Beef Patty 150g", 1, "unit"), ("Burger Bun", 1, "unit"), ("Cheddar Slice", 1, "unit"), ("Lettuce", 20, "g", "rm-lettuce"),
                                    ("Tomato", 30, "g", "rm-tomato"), ("Onion", 15, "g", "rm-onion"), ("House Sauce", 25, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Chicken Burger", [("Chicken Fillet", 1, "unit"), ("Burger Bun", 1, "unit"), ("Lettuce", 20, "g", "rm-lettuce"), ("Tomato", 30, "g", "rm-tomato"),
                               ("House Sauce", 20, "ml"), ("Cooking Oil", 40, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Double Smash Burger", [("Beef Patty 150g", 2, "unit"), ("Burger Bun", 1, "unit"), ("Cheddar Slice", 2, "unit"), ("Onion", 20, "g", "rm-onion"),
                                    ("House Sauce", 25, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Saffron Spiced Burger", [("Beef Patty 150g", 1, "unit"), ("Burger Bun", 1, "unit"), ("Onion", 40, "g", "rm-onion"), ("House Sauce", 25, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Quarter Chicken & Chips", [("Chicken Quarter", 1, "unit"), ("Potatoes", 220, "g"), ("Cooking Oil", 30, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Chicken Wings (6)", [("Chicken Wings", 6, "unit"), ("Cooking Oil", 40, "ml"), ("Takeaway Box", 1, "unit")]),
        rec("Chicken Shawarma Wrap", [("Shawarma Chicken", 120, "g"), ("Tortilla Wrap", 1, "unit"), ("Potatoes", 80, "g"), ("House Sauce", 20, "ml")]),
        rec("Falafel Wrap", [("Falafel", 4, "unit"), ("Tortilla Wrap", 1, "unit"), ("Lettuce", 25, "g"), ("Tomato", 30, "g")]),
        rec("Regular Chips", [("Potatoes", 220, "g"), ("Cooking Oil", 30, "ml")]),
        rec("Large Chips", [("Potatoes", 340, "g"), ("Cooking Oil", 45, "ml")]),
        rec("Onion Rings", [("Onion", 150, "g"), ("Cooking Oil", 40, "ml")]),
        rec("Garden Salad", [("Lettuce", 80, "g"), ("Tomato", 60, "g")]),
        rec("Coke 330ml", [("Coke 330ml Can", 1, "unit")]),
        rec("Fresh Lemonade", [("Lemons", 2, "unit")]),
        rec("Iced Coffee", [("Milk", 200, "ml"), ("Ice Cream", 60, "ml")]),
        rec("Milkshake", [("Milk", 250, "ml"), ("Ice Cream", 120, "ml")]),
    ]
    await db.flush()
    for r, lines in recipes:
        for ln in lines:
            db.add(RecipeItem(business_id=b.id, recipe_id=r.id, ingredient_id=I[ln[0]].id, quantity_milli=to_milli(ln[1], ln[2]),
                              removable_ref_id=ln[3] if len(ln) > 3 else None))
    await db.flush()

    # opening stock 36 days ago + weekly deliveries (enough to cover the period, ending near par so some items run low)
    t_open = NOW - timedelta(days=36)
    for ing in I.values():
        par = ing.par_level_milli or 0
        db.add(build_movement(business_id=b.id, ingredient_id=ing.id, quantity_milli=par * 2,
                              movement_type=StockMovementType.OPENING_BALANCE.value,
                              unit_cost_micro_cents=ing.cost_per_canonical_micro_cents, occurred_at=t_open))
    await db.flush()

    customers = []
    for i in range(140):
        nm = f"{FIRST[i % len(FIRST)]} {LAST[(i * 7) % len(LAST)]}"
        c = Customer(business_id=b.id, wa_id=f"2700000{1000 + i:04d}", display_name=nm, phone_number=f"+27000{1000 + i:04d}",
                     loyalty_points_balance=R.choice([0, 0, 40, 85, 120, 160, 240, 310, 520, 680, 1150]),
                     loyalty_tier="BRONZE", last_message_at=NOW - timedelta(days=R.randint(0, 30)))
        if c.loyalty_points_balance >= 500:
            c.loyalty_tier = "SILVER"
        customers.append(c)
    db.add_all(customers)
    await db.flush()
    return b, staff, items, I, customers


def build_line(item, qty):
    """One order line with realistic modifiers, returned as (OrderItem kwargs, ResolvedPOSItem)."""
    opts, add_ons, removed = [], [], []
    oj = item.options_json or {}
    for g in oj.get("option_groups", []):
        o = R.choice(g["options"])
        opts.append({"group_id": g["id"], "group_name": g["name"], "option_id": o["id"], "option_name": o["name"], "price_delta_cents": 0})
    if item.name in BURGERS and R.random() < 0.28:
        a = R.choice(ADD_ONS)
        add_ons.append({"name": a[0], "price_cents": money(a[1]), "qty": 1})
    if oj.get("removable_ingredients") and R.random() < 0.15:
        removed.append(R.choice(oj["removable_ingredients"]))
    add_total = sum(a["price_cents"] for a in add_ons)
    unit = item.price_cents + add_total
    bd = LineItemBreakdown(item.price_cents, 0, add_total, unit, qty, unit * qty)
    res = ResolvedPOSItem(menu_item_id=item.id, name=item.name, breakdown=bd, selected_options_snapshot=opts, add_ons_snapshot=add_ons,
                          removed_ingredients_snapshot=removed, special_instructions=None, requires_preparation=item.requires_preparation)
    kw = dict(menu_item_id=item.id, name_snapshot=item.name, base_price_cents=item.price_cents, option_adjustment_cents=0,
              add_on_total_cents=add_total, unit_price_cents=unit, quantity=qty, line_total_cents=unit * qty,
              selected_options_snapshot=opts or None, add_ons_snapshot=add_ons or None, removed_ingredients_snapshot=removed or None)
    return kw, res


async def history(db, b, staff, items, customers, *, days=35, base=70, pop=POP, whatsapp_share=0.46, seq_start=1):
    names, weights = zip(*[(n, w) for n, w in pop.items() if n in items])
    cashier_ids = [u.id for u in staff.values()]
    seq = seq_start - 1
    today_local = NOW.astimezone(SAST).date()
    for d in range(days, -1, -1):
        day = today_local - timedelta(days=d)
        dow = day.weekday()
        growth = 1 + 0.18 * (days - d) / days
        n = int(base * growth * (1.45 if dow in (4, 5) else 1.2 if dow == 6 else 1.0) * R.uniform(0.9, 1.1))
        if d == 0:   # today: only up to the current hour
            n = int(n * min(1.0, max(0.2, (NOW.astimezone(SAST).hour - 10) / 11)))
        for _ in range(n):
            h = R.choices([11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21], [4, 12, 13, 7, 4, 5, 9, 13, 12, 7, 3])[0]
            t = datetime(day.year, day.month, day.day, h, R.randint(0, 59), R.randint(0, 59), tzinfo=SAST).astimezone(timezone.utc)
            if t > NOW - timedelta(minutes=40):
                continue
            wa = R.random() < whatsapp_share
            cust = R.choice(customers) if (wa or R.random() < 0.3) else None
            k = R.choices([1, 2, 3, 4], [30, 38, 22, 10])[0]
            lines = {}
            for nm in R.choices(names, weights, k=k):
                lines[nm] = lines.get(nm, 0) + (2 if R.random() < 0.12 else 1)
            built = [build_line(items[nm], q) for nm, q in lines.items()]
            sub = sum(k_["line_total_cents"] for k_, _ in built)
            seq += 1
            cancelled = R.random() < 0.015
            mode = "PICKUP" if wa else R.choice(["PICKUP", "PICKUP", "DINE_IN"])
            status = "CANCELLED" if cancelled else ("COLLECTED" if mode == "PICKUP" else "COMPLETED")
            method = "CARD" if R.random() < (0.55 if not wa else 0.5) else "CASH"
            o = Order(business_id=b.id, customer_id=cust.id if cust else None, order_number=format_order_number(seq), status=status,
                      payment_status="VOIDED" if cancelled else "PAID", order_mode=mode, source="WHATSAPP" if wa else "POS",
                      subtotal_cents=sub, delivery_fee_cents=0, total_cents=sub, currency="ZAR",
                      tax_cents=int(round(sub * 15 / 115)), discount_cents=0,
                      customer_name=cust.display_name if cust else None, phone_number=cust.phone_number if cust else None,
                      confirmed_at=t, accepted_at=t + timedelta(minutes=1), ready_at=t + timedelta(minutes=R.randint(9, 18)),
                      completed_at=t + timedelta(minutes=R.randint(15, 25)), payment_required=False, payment_method=method,
                      paid_at=t + timedelta(minutes=20), created_by_user_id=None if wa else R.choice(cashier_ids))
            o.created_at = t
            o.updated_at = t
            db.add(o)
            await db.flush()
            for kw, _ in built:
                db.add(OrderItem(order_id=o.id, business_id=b.id, **kw))
            if not cancelled and b.inventory_enabled:
                await post_sale_consumption(db, business_id=b.id, order_id=o.id, resolved_items=[r for _, r in built], occurred_at=t)
        await db.commit()
    await db.execute(update(Business).where(Business.id == b.id).values(order_number_sequence=seq))
    await db.commit()
    return seq


async def weekly_deliveries(db, b, I):
    for wk in range(5, 0, -1):
        t = NOW - timedelta(days=wk * 7 - 1)
        for ing in I.values():
            par = ing.par_level_milli or 0
            db.add(build_movement(business_id=b.id, ingredient_id=ing.id, quantity_milli=int(par * R.uniform(0.85, 1.05)),
                                  movement_type=StockMovementType.GOODS_RECEIVED.value,
                                  unit_cost_micro_cents=ing.cost_per_canonical_micro_cents, occurred_at=t))
    await db.commit()


VENDORS = [
    ("Forno Nove Pizza", "forno-nove-pizza", ("#9F2B1F", "#F2C14E"), "owner@fornonove.example",
     [("Pizza", [("Margherita", 85, "Tomato, mozzarella, basil."), ("Chicken & Mushroom", 109, "Grilled chicken, mushrooms, mozzarella."),
                 ("Beef Pepperoni (halaal)", 115, "Beef pepperoni, mozzarella."), ("Four Cheese", 119, "Mozzarella, cheddar, feta, parmesan.")]),
      ("Drinks", [("Sparkling Water", 20, None), ("Coke 330ml", 22, None)])],
     {"Margherita": 10, "Chicken & Mushroom": 12, "Beef Pepperoni (halaal)": 9, "Four Cheese": 6, "Sparkling Water": 4, "Coke 330ml": 8}),
    ("Medina Sweets", "medina-sweets", ("#5B3A87", "#E9B949"), "owner@medinasweets.example",
     [("Desserts", [("Kunafa Slice", 55, "Warm kunafa, cream cheese, pistachio."), ("Baklava (4 pieces)", 48, "Walnut and pistachio."),
                    ("Churros & Chocolate", 45, None), ("Waffle with Ice Cream", 65, None)]),
      ("Drinks", [("Falooda", 49, "Rose milk, vermicelli, ice cream."), ("Mint Tea", 25, None)])],
     {"Kunafa Slice": 12, "Baklava (4 pieces)": 8, "Churros & Chocolate": 7, "Waffle with Ice Cream": 6, "Falooda": 9, "Mint Tea": 5}),
    ("Kettle & Bean Coffee", "kettle-and-bean-coffee", ("#1F5F4A", "#D9C7A7"), "owner@kettlebean.example",
     [("Coffee", [("Cappuccino", 34, None), ("Flat White", 36, None), ("Spanish Latte", 42, None), ("Iced Caramel Latte", 45, None)]),
      ("Bakes", [("Date Muffin", 32, None), ("Cinnamon Bun", 38, None)])],
     {"Cappuccino": 12, "Flat White": 7, "Spanish Latte": 10, "Iced Caramel Latte": 9, "Date Muffin": 5, "Cinnamon Bun": 6}),
]


async def vendors(db):
    out = []
    for name, slug, brand, email, spec, pop in VENDORS:
        b = await make_business(db, name=name, slug=slug, brand=brand, inventory=False)
        staff = await users(db, b, email, [f"{name} Owner", ("STAFF", "Stall Staff 1"), ("STAFF", "Stall Staff 2")])
        items = await menu(db, b, spec)
        db.add(LoyaltyProgram(business_id=b.id, is_enabled=False, points_per_rand=1, points_per_order=0, points_expiry_days=365,
                              min_redemption_points=100, silver_tier_points=500, gold_tier_points=2000))
        cust = [Customer(business_id=b.id, wa_id=f"2700002{i:04d}", display_name=f"{FIRST[(i * 5) % len(FIRST)]} {LAST[(i * 3) % len(LAST)]}",
                         phone_number=f"+27002{i:04d}") for i in range(40)]
        db.add_all(cust)
        await db.flush()
        await history(db, b, staff, items, cust, days=6, base=55, pop=pop, whatsapp_share=0.55)
        out.append(b)
    return out


async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    S = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with S() as db:
        b, staff, items, I, customers = await main_restaurant(db)
        await db.commit()
        await weekly_deliveries(db, b, I)
        seq = await history(db, b, staff, items, customers)
        # an open till for today (the POS requires one)
        db.add(TillSession(business_id=b.id, opened_by_user_id=list(staff.values())[1].id, opening_float_cents=money(500),
                           status="OPEN", opened_at=NOW - timedelta(hours=9)))
        await db.commit()
        vs = await vendors(db)
        print(f"Saffron Street Kitchen: code {b.business_code}, {seq} orders, {len(I)} ingredients, {len(items)} menu items")
        for v in vs:
            print(f"vendor {v.name}: code {v.business_code}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
