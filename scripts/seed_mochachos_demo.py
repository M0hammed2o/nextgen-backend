"""
Seed script for the Mochachos sales-demo tenant: "Mochachos - NextGen AI Demo".

Creates ONE new, standalone business configured as WhatsApp-only
(pos_enabled=False), with a regular menu, a Specials section, and a
"Mochachos Secret Menu - Demo" section, built from researched Mochachos SA
menu/pricing data (primary source: the Overport branch's Mr D Food listing,
2026-10-21 - see this task's report for full sourcing/confidence notes per
item). This script does NOT touch the protected WhatsApp AI pipeline - it
only inserts rows via the existing Business/MenuCategory/MenuItem/Special/
BusinessUser ORM models, the same ones every other onboarding path uses.

Idempotent: every entity is looked up by a stable natural key before being
created (same convention as scripts/seed_whatsapp_only_qa.py), so running
this twice is safe.

PRICING NOTE: regular-menu prices are sourced from the Mochachos Overport
Mr D Food delivery listing (branch-specific, "from" prices as of
2026-10-21) - the most relevant available source for this specific
branch, but still a delivery-platform price, not a confirmed in-store
price. Two of the three "Specials" are real sourced data (the official
"Meal of the Month" promo, price not published by Mochachos so a demo
price is used; and the Overport-specific "2 Chicken Fillets and a Side"
markdown, which IS a real sourced price). The third special and all
Secret Menu items are explicitly invented demo content, per the task's
own instruction for that section - never present these as confirmed real
Mochachos offers.

Run against a target database by setting DATABASE_URL, e.g. for a local
disposable test/dev stack:
  DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test" \
    python -m scripts.seed_mochachos_demo

To seed the real production tenant for the actual Mochachos meeting, run
this with the real production DATABASE_URL set in your own shell/environment
- never paste that connection string into a chat message or commit it
anywhere. This script never logs or prints DATABASE_URL.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._seed_safety import looks_local  # noqa: E402

OWNER_EMAIL = "owner@mochachos-demo.nextgenintelligence.co.za"
MANAGER_EMAIL = "manager@mochachos-demo.nextgenintelligence.co.za"
STAFF_NAME = "Mochachos Demo Staff"

BUSINESS_SLUG = "mochachos-nextgen-ai-demo"

# -- Credentials: never hardcoded -----------------------------------------
# Supply your own via env vars (MOCHACHOS_OWNER_PASSWORD,
# MOCHACHOS_MANAGER_PASSWORD, MOCHACHOS_STAFF_PIN - 4 digits). If a var is
# unset, a cryptographically random one is generated at run time and
# printed ONCE, to this terminal only, at the very end of a real (non
# dry-run) run - never written to a file, never included in any commit,
# log line, or exception message.


def _generate_password() -> str:
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _generate_pin() -> str:
    import secrets

    return f"{secrets.randbelow(10000):04d}"


def _resolve_credential(env_var: str, generator) -> tuple[str, bool]:
    """Returns (value, was_generated)."""
    value = os.environ.get(env_var)
    if value:
        return value, False
    return generator(), True

# Spice levels - official, verified-current (mochachos.com homepage copy,
# cross-confirmed against a 2017 News24 advertorial naming the same five).
SPICE_LEVEL_OPTION_GROUP = {
    "id": "spice-level",
    "name": "Spice Level",
    "required": True,
    "min_selections": 1,
    "max_selections": 1,
    "sort_order": 0,
    "is_enabled": True,
    "default_option_id": "mild",
    "options": [
        {"id": "lemon-herb", "name": "Lemon and Herb", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
        {"id": "mild", "name": "Mild", "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
        {"id": "hot", "name": "Hot", "price_delta_cents": 0, "sort_order": 2, "is_enabled": True},
        {"id": "extra-hot", "name": "Extra Hot", "price_delta_cents": 0, "sort_order": 3, "is_enabled": True},
        {"id": "dynamite", "name": "Dynamite", "price_delta_cents": 0, "sort_order": 4, "is_enabled": True},
    ],
}

# -- Regular menu ----------------------------------------------------------
# (category_name, sort_order, [(item_name, description, price_cents, has_spice_option)])
REGULAR_MENU = [
    ("Flame-Grilled Chicken", 0, [
        ("Quarter Chicken", "Butterflied, flame-grilled, flavoured with a blend of herbs and spices", 5990, True),
        ("Half Chicken", "Butterflied, flame-grilled", 11490, True),
        ("Full Chicken", "Butterflied, flame-grilled", 21990, True),
    ]),
    ("Chicken Meals", 1, [
        ("Bambino Meal: Jose", "Kids meal - chips, fruit juice, free toy", 8490, True),
        ("Bambino Meal: Pedro", "Kids meal - chips, fruit juice, free toy", 8490, True),
        ("Bambino Meal: Speedy", "Kids meal - chips, fruit juice, free toy", 8490, True),
        ("Bambino Meal: Juanita", "Kids meal - chips, fruit juice, free toy", 8490, True),
        ("2 Chicken Fillets and a Side", "Two flame-grilled fillets with a side of your choice", 9990, True),
    ]),
    ("Burgers", 2, [
        ("Single Chicken Burger", "Flame-grilled chicken fillet", 6990, True),
        ("Single Gourmet Chicken Burger", "Feta, guacamole, jalapeno", 8490, True),
        ("Single Jalapeno Cheese Burger", "Chicken fillet, jalapenos, cheese", 7990, True),
        ("Single Chicken Cheese Burger", "Chicken fillet, cheese", 7990, True),
        ("Single Chicken Cheese and Pine Burger", "Chicken fillet, cheese, pineapple", 8990, True),
        ("Veggie Burger", "Meat-free patty", 6990, False),
        ("Single Gourmet Veggie Burger", "Meat-free patty, feta, guacamole, jalapeno", 8490, False),
        ("Double Chicken Burger", "Two flame-grilled chicken fillets", 8990, True),
        ("Double Gourmet Chicken Burger", "Two fillets, feta, guacamole, jalapeno", 10490, True),
        ("Double Jalapeno Cheese Burger", "Two fillets, jalapenos, cheese", 9990, True),
        ("Double Chicken Cheese Burger", "Two fillets, cheese", 9990, True),
        ("Beef Burger", "150g beef patty, lettuce, tomato", 7990, False),
        ("Gourmet Beef Burger", "Beef patty, feta, guacamole, jalapeno", 9490, False),
        ("Jalapeno Cheese Beef Burger", "Beef patty, jalapenos, cheese", 8990, False),
        ("Mexican Lamb Burger", "Lamb patty, Mexican-style toppings", 7990, False),
        ("Nacho Crunch Gourmet Burger", "Chicken fillet coated in nachos, feta, guacamole, sour cream, salsa, jalapeno", 8990, True),
        ("Chicken Strip and Cheese Burger", "Five chicken strips, cheese, lettuce, tomato", 7990, True),
        ("Mexican Crumbed Burger", "Breadcrumbed chicken, herbs and spices", 6990, False),
    ]),
    ("Schwarmas, Burritos and Quesadillas", 3, [
        ("Chicken Schwarma", "Flame-grilled chicken, rotisserie-style, wrapped in flatbread", 9490, True),
        ("Rump Steak Schwarma", "Rump steak, rotisserie-style, wrapped in flatbread", 10490, False),
        ("Veggie Schwarma", "Meat-free filling, rotisserie-style, wrapped in flatbread", 9490, False),
        ("Chicken Burrito", "Chicken, onion, green pepper, cheddar, guacamole", 8990, True),
        ("Chicken Quesadilla", "Toasted tortilla, chicken, onion, pepper, cheddar, mayo", 12990, True),
    ]),
    ("Combos", 4, [
        ("Chicken Burger, Beeg Chips and a Buddy", "Chicken burger, chips, 440ml soft drink", 11990, True),
        ("Quarter Chicken, Beeg Chips and a Buddy", "Quarter chicken, chips, 440ml soft drink", 10990, True),
        ("Quarter Chicken and Beeg Chips", "Quarter chicken, chips", 8690, True),
        ("Quarter Chicken, Pap and Mexican Sauce", "Quarter chicken, pap, Mexican sauce", 7690, True),
        ("Half Chicken, Beeg Chips and a Buddy", "Half chicken, chips, 440ml soft drink", 15990, True),
        ("4 Full BBQ Wings and Beeg Chips", "Four BBQ-basted wings, chips", 10990, False),
        ("8 Full BBQ Wings and Beeger Chips", "Eight BBQ-basted wings, large chips", 18990, False),
        ("Chicken Burrito, Beeg Chips and a Buddy", "Chicken burrito, chips, 440ml soft drink", 13990, True),
        ("Rump Steak Burrito, Beeg Chips and a Buddy", "Rump steak burrito, chips, 440ml soft drink", 14990, False),
        ("Schwarma, Beeg Chips and a Buddy", "Schwarma of your choice, chips, 440ml soft drink", 14490, True),
        ("Amigo Combo", "Half chicken, a side, two buns", 15990, True),
        ("Nacho Crunch Gourmet Burger Combo", "Nacho Crunch Gourmet Burger, chips, drink", 13490, True),
    ]),
    ("Family Meals", 5, [
        ("Family Variety", "Half chicken, two chicken burgers, largest chips", 29990, True),
        ("Family Feast", "Full chicken, largest chips, four buns", 30990, True),
        ("Family Fiesta", "Full chicken, largest chips, largest coleslaw or salad, four buns", 33990, True),
        ("Family Share Meal", "Two chicken burgers, two kiddies chicken burgers, six chicken strips with dip, a side", 33990, True),
        ("Family Treat", "Two full chickens, two largest chips, eight buns, largest coleslaw or salad", 61990, True),
    ]),
    ("Sides", 6, [
        ("Chips", "", 3590, False),
        ("Loaded Chips", "Chip spice, cheese sauce, jalapenos", 4790, False),
        ("Potato Wedges", "", 3590, False),
        ("Onion Rings", "", 2990, False),
        ("Mexican Rice", "", 3590, False),
        ("Pap and Mexican Sauce", "", 3290, False),
        ("Spinach and Butternut", "Creamed spinach and butternut", 4290, False),
        ("Dipz", "Choice of sauce", 1190, False),
        ("Bun", "", 1190, False),
    ]),
    ("Sauces and Extras", 7, [
        ("Mochachos Sauce 250ml", "Bottled to take home", 4590, False),
        ("Extra Dip", "Salsa, guacamole, peri-mayo or sweet chilli", 1190, False),
        ("Add Cheese or Pineapple", "Burger topping add-on", 1000, False),
    ]),
    ("Drinks", 8, [
        ("Buddy 440ml", "Soft drink", 2890, False),
        ("Still or Sparkling Water", "", 2690, False),
        ("Appletiser or Grapetiser", "", 3490, False),
        ("1.5 Litre Soft Drink", "", 3990, False),
        ("Cappy Juice 330ml", "", 3490, False),
        ("Freezo", "Coffee, Toffee Caramel or White Chocolate, 350ml", 4990, False),
        ("Milkshake", "Chocolate, Strawberry, Banana, Bubblegum or Lime, 350ml", 4490, False),
        ("Americano", "", 2690, False),
        ("Cappuccino or Cafe Latte", "", 3290, False),
        ("Espresso", "", 2690, False),
        ("Rooibos or Five Roses Tea", "", 2790, False),
        ("Homemade Quencher", "Peach or Lemon Ice Tea, Ginger Beer, or Lemonade, 350ml", 3490, False),
    ]),
]

# -- Specials --------------------------------------------------------------
# (title, description, price_cents or None, is_real_sourced)
SPECIALS_MENU_ITEMS = [
    (
        "Meal of the Month: 8 Full Wings",
        "Flame-grilled wings, spiced to your preference, basted in BBQ sauce, served with Beeger chips. "
        "DEMO PRICE - Mochachos' official site lists this as the current featured promo but does not publish a price.",
        9990,
    ),
    (
        "2 Chicken Fillets and a Side - Special",
        "Real Overport-branch markdown as listed on Mr D Food on 2026-10-21 (was R99.90).",
        8990,
    ),
    (
        "Weekday Family Fiesta Special",
        "ILLUSTRATIVE DEMO PROMO - not a confirmed real current Mochachos offer. Built from the real Family Fiesta "
        "meal at a plausible weekday discount, for demo purposes only.",
        29990,
    ),
]

SECRET_MENU_ITEMS = [
    ("The Dynamite Double Stack", "DEMO ITEM - Double Chicken Burger upgraded with an extra Dynamite-spiced fillet, jalapenos and extra cheese.", 11990),
    ("Build-Your-Own Schwarma", "DEMO ITEM - pick your protein plus three toppings.", 9990),
    ("Chicken and Waffle Tower", "DEMO ITEM - secret-menu-exclusive combination.", 10990),
    ("The Secret Sauce Sampler", "DEMO ITEM - all four dip sauces with a side of loaded chips.", 5990),
    ("Midnight Munchies Box", "DEMO ITEM - mixed wings, chips, two dips, a drink.", 13990),
]


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print exactly what would be created, without writing anything. No commit is issued.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Required to actually write when DATABASE_URL's host does not look like a local/disposable one "
             "(see scripts/_seed_safety.py looks_local). Has no effect with --dry-run.",
    )
    return parser.parse_args()




async def seed(dry_run: bool, confirmed: bool) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core.security import hash_password, hash_pin
    from shared.models import Business, BusinessUser, MenuCategory, MenuItem
    from shared.models.specials import Special
    from shared.utils import generate_business_code

    database_url = os.environ["DATABASE_URL"]
    local_target = looks_local(database_url)
    print(f"Target environment: {'local/disposable (matched a known local marker)' if local_target else 'NOT recognised as local - treating as a real/remote database'}")
    if not local_target and not dry_run and not confirmed:
        print(
            "\nRefusing to write: DATABASE_URL does not look like a local/disposable database, "
            "and --dry-run was not passed. If you really mean to seed this target, re-run with --yes.\n"
            "(This check only looks at whether the host matches a known local marker - it never reads "
            "or prints the connection string itself.)",
            file=sys.stderr,
        )
        # Non-zero, like scripts/_seed_safety.require_local_or_confirmed, so a
        # wrapper or CI step never mistakes a refusal for a successful seed.
        raise SystemExit(2)
    if dry_run:
        print("DRY RUN - no writes will be committed.\n")

    owner_password, owner_generated = _resolve_credential("MOCHACHOS_OWNER_PASSWORD", _generate_password)
    manager_password, manager_generated = _resolve_credential("MOCHACHOS_MANAGER_PASSWORD", _generate_password)
    staff_pin, staff_pin_generated = _resolve_credential("MOCHACHOS_STAFF_PIN", _generate_pin)
    if not (len(staff_pin) == 4 and staff_pin.isdigit()):
        # The staff till's PinKeypad is fixed at 4 digits; any other PIN is unusable.
        print("MOCHACHOS_STAFF_PIN must be exactly 4 digits (value not shown).", file=sys.stderr)
        raise SystemExit(2)

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        biz_result = await db.execute(select(Business).where(Business.slug == BUSINESS_SLUG))
        business = biz_result.scalar_one_or_none()

        # -- Non-destructive guard: if a business already owns this slug,
        #    this script must NEVER touch its WhatsApp connection fields,
        #    its plan/pos_enabled flags, or any other pre-existing value -
        #    it only adds missing menu/staff/specials rows underneath it.
        #    This also means re-running this script can never reassign or
        #    clear a real WhatsApp connection another process has since set.
        existing_business = business is not None
        if existing_business and business.whatsapp_phone_number_id:
            print(
                f"[guard] Business '{BUSINESS_SLUG}' already has a WhatsApp number connected "
                f"(phone_number_id ends in ...{business.whatsapp_phone_number_id[-4:]}). "
                "This script will NOT modify that connection - proceeding with menu/staff data only."
            )

        if existing_business:
            print(f"[exists]  Business already present (code: {business.business_code}, id: {business.id})")
        else:
            business = Business(
                name="Mochachos - NextGen AI Demo",
                slug=BUSINESS_SLUG,
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                currency="ZAR",
                # Real, verified-current address (mochachos.com/store/overport/, 2026-10-21).
                address="358 Brickfield Road, Overport Centre, Overport, Durban, 4091",
                phone="031 207 8636",
                delivery_enabled=False,
                require_customer_name=False,
                require_phone_number=True,
                require_delivery_address=False,
                payment_provider="MOCK",
                # Left null deliberately - no WhatsApp number is connected yet.
                # Set this to the approved demo number once that decision is made.
                whatsapp_phone_number_id=None,
                is_whatsapp_enabled=True,
                billing_status="TRIAL",
                plan="WHATSAPP_ONLY",
                pos_enabled=False,
                whatsapp_order_allowance=1000,
                greeting_text="Welcome to Mochachos! Flame-grilled chicken, your way. What can I get started for you today?",
                closed_text="Thanks for messaging Mochachos! We're currently closed - please check back during our opening hours.",
            )
            db.add(business)
            await db.flush()
            tag = "[would create]" if dry_run else "[created]"
            print(f"{tag} Business: {business.name} (code: {business.business_code}, id: {business.id})")

        async def ensure_staff(email, staff_name, role, password, pin):
            """Returns (user, was_created). The caller must only treat `password`/
            `pin` as the real, in-effect credential when was_created is True -
            an existing row's password/PIN is never touched here, so a
            generated-but-unused value must never be reported as live."""
            query = select(BusinessUser).where(BusinessUser.business_id == business.id, BusinessUser.role == role)
            query = query.where(BusinessUser.email == email) if email else query.where(BusinessUser.staff_name == staff_name)
            existing = (await db.execute(query)).scalar_one_or_none()
            if existing:
                print(f"[exists]  {role}: {email or staff_name}")
                return existing, False
            user = BusinessUser(
                business_id=business.id, role=role, staff_name=staff_name, email=email,
                password_hash=hash_password(password) if password else None,
                pin_hash=hash_pin(pin) if pin else None,
                pin_updated_at=datetime.now(timezone.utc) if pin else None,
                is_active=True, must_change_password=False,
            )
            db.add(user)
            print(f"{'[would create]' if dry_run else '[created]'} {role}: {email or staff_name}")
            return user, True

        _, owner_applied = await ensure_staff(OWNER_EMAIL, "Mochachos Demo Owner", "OWNER", owner_password, None)
        _, manager_applied = await ensure_staff(MANAGER_EMAIL, "Mochachos Demo Manager", "MANAGER", manager_password, None)
        _, staff_applied = await ensure_staff(None, STAFF_NAME, "STAFF", None, staff_pin)

        counts = {"categories_created": 0, "categories_existing": 0, "items_created": 0, "items_existing": 0}

        async def ensure_category(name, sort_order):
            existing = (await db.execute(
                select(MenuCategory).where(MenuCategory.business_id == business.id, MenuCategory.name == name)
            )).scalar_one_or_none()
            if existing:
                counts["categories_existing"] += 1
                return existing
            cat = MenuCategory(business_id=business.id, name=name, sort_order=sort_order)
            db.add(cat)
            await db.flush()
            counts["categories_created"] += 1
            print(f"{'[would create]' if dry_run else '[created]'} Category: {name}")
            return cat

        async def ensure_item(category, name, description, price_cents, sort_order, options_json=None):
            existing = (await db.execute(
                select(MenuItem).where(MenuItem.business_id == business.id, MenuItem.name == name)
            )).scalar_one_or_none()
            if existing:
                counts["items_existing"] += 1
                return existing
            item = MenuItem(
                business_id=business.id, category_id=category.id, name=name,
                description=description or None, price_cents=price_cents, sort_order=sort_order,
                options_json={"option_groups": [options_json]} if options_json else None,
            )
            db.add(item)
            await db.flush()
            counts["items_created"] += 1
            print(f"{'[would create]' if dry_run else '[created]'} Menu item: {name} (R{price_cents / 100:.2f})")
            return item

        # -- Regular menu --
        for cat_name, cat_sort, items in REGULAR_MENU:
            category = await ensure_category(cat_name, cat_sort)
            for i, (item_name, description, price_cents, has_spice) in enumerate(items):
                await ensure_item(
                    category, item_name, description, price_cents, i,
                    SPICE_LEVEL_OPTION_GROUP if has_spice else None,
                )

        regular_item_count = sum(len(items) for _, _, items in REGULAR_MENU)

        # -- Specials: real MenuItems (orderable) + Special rows (so the bot
        #    can mention them when asked, per prompt_builder.py's existing
        #    "today's specials" reference-context behaviour) --
        specials_category = await ensure_category("Mochachos Specials", 100)
        for i, (title, description, price_cents) in enumerate(SPECIALS_MENU_ITEMS):
            await ensure_item(specials_category, title, description, price_cents, i)

        for i, (title, description, _price_cents) in enumerate(SPECIALS_MENU_ITEMS):
            existing_special = (await db.execute(
                select(Special).where(Special.business_id == business.id, Special.title == title)
            )).scalar_one_or_none()
            if existing_special:
                continue
            db.add(Special(business_id=business.id, title=title, description=description, sort_order=i, is_active=True))
            print(f"{'[would create]' if dry_run else '[created]'} Special: {title}")

        # -- Secret menu - explicitly demo-only, per the task's own framing --
        secret_category = await ensure_category("Mochachos Secret Menu - Demo", 200)
        for i, (name, description, price_cents) in enumerate(SECRET_MENU_ITEMS):
            await ensure_item(secret_category, name, description, price_cents, i)

        # -- Single commit point. A dry run rolls back instead: every
        #    "would create"/"exists" line printed above came from real
        #    SELECTs against this same transaction (including rows this
        #    same run just flushed), so the report is accurate, but nothing
        #    is ever persisted when --dry-run is set. --
        if dry_run:
            await db.rollback()
            print("\nDRY RUN complete - nothing was written.")
        else:
            await db.commit()
            print("\nSeed complete.")

    await engine.dispose()
    print(f"Business slug: {BUSINESS_SLUG}")
    print(f"Regular menu items (expected total): {regular_item_count}")
    print(f"Specials items (expected total): {len(SPECIALS_MENU_ITEMS)}")
    print(f"Secret menu items (expected total): {len(SECRET_MENU_ITEMS)}")
    print(
        f"Categories: {counts['categories_created']} {'would be created' if dry_run else 'created'}, "
        f"{counts['categories_existing']} already existed (untouched)"
    )
    print(
        f"Menu items: {counts['items_created']} {'would be created' if dry_run else 'created'}, "
        f"{counts['items_existing']} already existed (untouched)"
    )
    if dry_run:
        return

    # Only report a credential as live if it was actually just applied to a
    # newly-created row this run - an existing staff member's password/PIN
    # is never touched here, so a generated-but-unused value must never be
    # reported as though it were the real, in-effect credential (a
    # generated value is recomputed every run regardless of whether it's
    # used, so printing it unconditionally would show a DIFFERENT, bogus
    # password on every re-run while the real stored one never changes).
    newly_applied_generated = any([
        owner_applied and owner_generated,
        manager_applied and manager_generated,
        staff_applied and staff_pin_generated,
    ])
    if newly_applied_generated:
        print(
            "\nThe following credentials were randomly generated (not supplied via env) and are shown "
            "ONCE here - they are not logged or written anywhere else. Store them yourself if you need them again:"
        )
    if owner_applied and owner_generated:
        print(f"  Owner:   {OWNER_EMAIL} / {owner_password}")
    elif owner_applied:
        print(f"  Owner:   {OWNER_EMAIL} / (password supplied via MOCHACHOS_OWNER_PASSWORD)")
    if manager_applied and manager_generated:
        print(f"  Manager: {MANAGER_EMAIL} / {manager_password}")
    elif manager_applied:
        print(f"  Manager: {MANAGER_EMAIL} / (password supplied via MOCHACHOS_MANAGER_PASSWORD)")
    if staff_applied and staff_pin_generated:
        print(f"  Staff:   {STAFF_NAME} / PIN {staff_pin}")
    elif staff_applied:
        print(f"  Staff:   {STAFF_NAME} / (PIN supplied via MOCHACHOS_STAFF_PIN)")
    if not (owner_applied or manager_applied or staff_applied):
        print("All staff accounts already existed - no new credentials were generated or applied this run.")


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(seed(dry_run=args.dry_run, confirmed=args.yes))
