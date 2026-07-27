"""
Demo/user-testing seed script — creates a super admin plus the "Barn Owl"
demo restaurant (owner, manager, cashier, test customers, menu, loyalty,
specials, trial subscription) for local user-acceptance testing.

Idempotent: every entity is looked up by a stable natural key (email, PIN
owner+name, business slug, category/item name, reward name, special title,
customer phone) before being created, so running this script twice never
duplicates data — it only fills in whatever is still missing. Safe to run
after every local restart.

Run: python -m scripts.seed
Requires DATABASE_URL to point at a reachable Postgres with migrations
already applied (see start-nextgen-testing.ps1, which does both before
calling this script).
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


DEMO_OWNER_PASSWORD = "BarnOwl2026!Owner"
DEMO_MANAGER_PASSWORD = "BarnOwl2026!Manager"
DEMO_CASHIER_PIN = "1234"
DEMO_SUPER_ADMIN_PASSWORD = "NextGen2026!Admin"


async def seed() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core.config import get_settings
    from backend.app.core.security import hash_password, hash_pin
    from shared.models import (
        AdminUser,
        Business,
        BusinessUser,
        Customer,
        LoyaltyProgram,
        LoyaltyReward,
        MenuAddOn,
        MenuCategory,
        MenuItem,
        Special,
    )
    from shared.models.menu import menu_item_add_ons
    from shared.utils import generate_business_code

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # ── Super admin (platform administrator login) ───────────────────
        existing_admin = await db.execute(
            select(AdminUser).where(AdminUser.email == "admin@nextgendemo.co.za")
        )
        if not existing_admin.scalar_one_or_none():
            db.add(AdminUser(
                email="admin@nextgendemo.co.za",
                password_hash=hash_password(DEMO_SUPER_ADMIN_PASSWORD),
                display_name="Super Admin (Demo)",
                role="SUPER_ADMIN",
            ))
            print(f"[created] Super admin: admin@nextgendemo.co.za / {DEMO_SUPER_ADMIN_PASSWORD}")
        else:
            print("[exists]  Super admin already present")
        await db.commit()

        # ── Barn Owl demo business ─────────────────────────────────────────
        biz_result = await db.execute(select(Business).where(Business.slug == "barn-owl-demo"))
        business = biz_result.scalar_one_or_none()
        if not business:
            business = Business(
                name="Barn Owl Cafe (DEMO)",
                slug="barn-owl-demo",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                currency="ZAR",
                business_hours={
                    "mon": {"open": "07:00", "close": "17:00"},
                    "tue": {"open": "07:00", "close": "17:00"},
                    "wed": {"open": "07:00", "close": "17:00"},
                    "thu": {"open": "07:00", "close": "17:00"},
                    "fri": {"open": "07:00", "close": "18:00"},
                    "sat": {"open": "08:00", "close": "14:00"},
                    "sun": None,
                },
                greeting_text="Welcome to Barn Owl Cafe (DEMO)! How can we help you today?",
                fallback_text="Sorry, I didn't catch that. Say 'menu' to see our menu.",
                closed_text="We're closed right now — Mon-Fri 7am-5/6pm, Sat 8am-2pm, closed Sundays.",
                address="12 Oak Avenue, Cape Town, 8001 (DEMO)",
                phone="+27210000000",
                delivery_enabled=True,
                delivery_fee_cents=2500,
                payment_provider="MOCK",
                # Without at least PAYMENT_LINK enabled, the Manager
                # Dashboard's Settings page never renders the payment
                # provider selector at all (a real, intentional UI gate --
                # found while browser-testing the owner dashboard). A demo
                # tenant meant to exercise MOCK online payments needs this
                # set so that section is actually visible to test.
                payment_methods_enabled=["PAYMENT_LINK"],
                max_staff_discount_cents=2000,  # R20 — lets the demo cashier test a small discount directly
                billing_status="TRIAL",
                trial_ends_at=datetime.now(timezone.utc) + timedelta(days=30),
                plan="STARTER",
            )
            db.add(business)
            await db.flush()
            print(f"[created] Business: {business.name} (code: {business.business_code}, slug: {business.slug})")
        else:
            print(f"[exists]  Business already present (code: {business.business_code}, slug: {business.slug})")
            # Self-healing for fields added to this script after Barn Owl
            # already existed on a given machine -- the "if not business"
            # block above only ever runs once, so a brand-new default like
            # payment_methods_enabled would otherwise never reach an
            # already-seeded tenant. Keeps the script idempotent in spirit:
            # re-running it always converges on the same fully-correct state.
            if not business.payment_methods_enabled:
                business.payment_methods_enabled = ["PAYMENT_LINK"]
                print("[updated] Business: payment_methods_enabled set to ['PAYMENT_LINK']")

        # ── Staff (owner / manager / cashier) ───────────────────────────────
        async def ensure_staff(email: str | None, staff_name: str, role: str, password: str | None, pin: str | None):
            query = select(BusinessUser).where(BusinessUser.business_id == business.id, BusinessUser.role == role)
            if email:
                query = query.where(BusinessUser.email == email)
            else:
                query = query.where(BusinessUser.staff_name == staff_name)
            existing = (await db.execute(query)).scalar_one_or_none()
            if existing:
                print(f"[exists]  {role}: {email or staff_name}")
                return existing
            user = BusinessUser(
                business_id=business.id,
                role=role,
                staff_name=staff_name,
                email=email,
                password_hash=hash_password(password) if password else None,
                pin_hash=hash_pin(pin) if pin else None,
                pin_updated_at=datetime.now(timezone.utc) if pin else None,
                is_active=True,
                must_change_password=False,  # demo/testing accounts — no forced reset friction
            )
            db.add(user)
            if email:
                print(f"[created] {role}: {email} / {password}")
            else:
                print(f"[created] {role}: {staff_name} — business code {business.business_code} + PIN {pin}")
            return user

        await ensure_staff("owner@barnowldemo.co.za", "Demo Owner", "OWNER", DEMO_OWNER_PASSWORD, None)
        await ensure_staff("manager@barnowldemo.co.za", "Demo Manager", "MANAGER", DEMO_MANAGER_PASSWORD, None)
        await ensure_staff(None, "Demo Cashier", "STAFF", None, DEMO_CASHIER_PIN)
        await db.commit()

        # ── Menu: categories + items (one with a required option group) ────
        async def ensure_category(name: str, sort_order: int) -> MenuCategory:
            existing = (await db.execute(
                select(MenuCategory).where(MenuCategory.business_id == business.id, MenuCategory.name == name)
            )).scalar_one_or_none()
            if existing:
                print(f"[exists]  Category: {name}")
                return existing
            cat = MenuCategory(business_id=business.id, name=name, sort_order=sort_order)
            db.add(cat)
            await db.flush()
            print(f"[created] Category: {name}")
            return cat

        coffee_cat = await ensure_category("Coffee & Drinks", 0)
        breakfast_cat = await ensure_category("Breakfast", 1)
        mains_cat = await ensure_category("Mains", 2)

        async def ensure_item(category: MenuCategory, name: str, price_cents: int, description: str | None = None,
                               options_json: dict | None = None, sort_order: int = 0) -> MenuItem:
            existing = (await db.execute(
                select(MenuItem).where(MenuItem.business_id == business.id, MenuItem.name == name)
            )).scalar_one_or_none()
            if existing:
                print(f"[exists]  Menu item: {name}")
                return existing
            item = MenuItem(
                business_id=business.id, category_id=category.id, name=name,
                price_cents=price_cents, description=description,
                options_json=options_json, sort_order=sort_order,
            )
            db.add(item)
            await db.flush()
            print(f"[created] Menu item: {name} (R{price_cents / 100:.2f})")
            return item

        cappuccino = await ensure_item(
            coffee_cat, "Cappuccino", 3200,
            description="Espresso, steamed milk, foam — the Barn Owl house blend.",
            options_json={
                "option_groups": [{
                    "id": "size", "name": "Size", "required": True,
                    "min_selections": 1, "max_selections": 1,
                    "options": [
                        {"id": "small", "name": "Small", "price_delta_cents": -500},
                        {"id": "regular", "name": "Regular", "price_delta_cents": 0},
                        {"id": "large", "name": "Large", "price_delta_cents": 700},
                    ],
                }],
            },
            sort_order=0,
        )
        await ensure_item(coffee_cat, "Filter Coffee", 2200, "Freshly brewed filter coffee.", sort_order=1)
        await ensure_item(coffee_cat, "Fresh Orange Juice", 2800, "Cold-pressed orange juice.", sort_order=2)
        await ensure_item(
            breakfast_cat, "Avo on Toast", 6500,
            description="Sourdough, smashed avo, feta, chilli flakes.", sort_order=0,
        )
        await ensure_item(breakfast_cat, "Full Breakfast", 8900, "Eggs, bacon, sausage, toast, grilled tomato.", sort_order=1)
        toastie = await ensure_item(mains_cat, "Ham & Cheese Toastie", 5500, "Grilled sourdough toastie.", sort_order=0)
        await ensure_item(mains_cat, "Chicken Mayo Sandwich", 6200, sort_order=1)
        await db.commit()

        # ── Add-ons (attached to the Cappuccino as a realistic example) ────
        async def ensure_add_on(name: str, price_cents: int) -> MenuAddOn:
            existing = (await db.execute(
                select(MenuAddOn).where(MenuAddOn.business_id == business.id, MenuAddOn.name == name)
            )).scalar_one_or_none()
            if existing:
                print(f"[exists]  Add-on: {name}")
                return existing
            add_on = MenuAddOn(business_id=business.id, name=name, price_cents=price_cents, min_qty=0, max_qty=3, default_qty=1)
            db.add(add_on)
            await db.flush()
            print(f"[created] Add-on: {name} (+R{price_cents / 100:.2f})")
            return add_on

        extra_shot = await ensure_add_on("Extra Espresso Shot", 1000)
        extra_cheese = await ensure_add_on("Extra Cheese", 1200)
        await db.commit()

        for item, add_on in ((cappuccino, extra_shot), (toastie, extra_cheese)):
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            await db.execute(
                pg_insert(menu_item_add_ons).values(menu_item_id=item.id, add_on_id=add_on.id).on_conflict_do_nothing()
            )
        await db.commit()

        # ── Special ──────────────────────────────────────────────────────
        existing_special = (await db.execute(
            select(Special).where(Special.business_id == business.id, Special.title == "Tuesday Toastie Special")
        )).scalar_one_or_none()
        if not existing_special:
            db.add(Special(
                business_id=business.id, title="Tuesday Toastie Special",
                description="Any toastie + a filter coffee for R65 — Tuesdays only.",
                days_of_week=["tue"], sort_order=0,
            ))
            print("[created] Special: Tuesday Toastie Special")
        else:
            print("[exists]  Special already present")
        await db.commit()

        # ── Loyalty programme + rewards ─────────────────────────────────────
        existing_program = (await db.execute(
            select(LoyaltyProgram).where(LoyaltyProgram.business_id == business.id)
        )).scalar_one_or_none()
        if not existing_program:
            db.add(LoyaltyProgram(
                business_id=business.id, is_enabled=True,
                points_per_rand=1, points_per_order=5,
                points_expiry_days=365, min_redemption_points=50,
                silver_tier_points=500, gold_tier_points=2000,
            ))
            print("[created] Loyalty programme (enabled, 1 point/Rand + 5/order)")
        else:
            print("[exists]  Loyalty programme already present")
        await db.commit()

        async def ensure_reward(name: str, points_cost: int, description: str):
            existing = (await db.execute(
                select(LoyaltyReward).where(LoyaltyReward.business_id == business.id, LoyaltyReward.name == name)
            )).scalar_one_or_none()
            if existing:
                print(f"[exists]  Loyalty reward: {name}")
                return
            db.add(LoyaltyReward(business_id=business.id, name=name, description=description, points_cost=points_cost))
            print(f"[created] Loyalty reward: {name} ({points_cost} pts)")

        await ensure_reward("Free Filter Coffee", 100, "Redeem for one free filter coffee.")
        await ensure_reward("Free Pastry", 150, "Redeem for one free pastry.")
        await db.commit()

        # ── Test customers (walk-in / WhatsApp identities, no login) ───────
        async def ensure_customer(phone: str, display_name: str):
            existing = (await db.execute(
                select(Customer).where(Customer.business_id == business.id, Customer.phone_number == phone)
            )).scalar_one_or_none()
            if existing:
                print(f"[exists]  Test customer: {display_name} ({phone})")
                return
            db.add(Customer(business_id=business.id, phone_number=phone, display_name=display_name))
            print(f"[created] Test customer: {display_name} ({phone})")

        await ensure_customer("0821110001", "Test Customer One")
        await ensure_customer("0821110002", "Test Customer Two")
        await db.commit()

    await engine.dispose()
    print("\nSeed complete.")
    print(f"Business code for staff PIN login: {business.business_code}")


if __name__ == "__main__":
    asyncio.run(seed())
