"""
Seed script — creates initial super admin user and a test business.
Run: python -m scripts.seed
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def seed():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from backend.app.core.config import get_settings
    from backend.app.core.security import hash_password, hash_pin
    from shared.models import (
        AdminUser, Base, Business, BusinessUser, MenuCategory, MenuItem,
    )
    from shared.utils import generate_business_code

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # ── Super Admin ──────────────────────────────────────────────────
        from sqlalchemy import select
        existing = await db.execute(
            select(AdminUser).where(AdminUser.email == "admin@nextgen.co.za")
        )
        if not existing.scalar_one_or_none():
            admin = AdminUser(
                email="admin@nextgen.co.za",
                password_hash=hash_password("Admin123!@#"),
                display_name="Super Admin",
                role="SUPER_ADMIN",
            )
            db.add(admin)
            print("✓ Created super admin: admin@nextgen.co.za / Admin123!@#")
        else:
            print("· Super admin already exists")

        # ── Test Business ────────────────────────────────────────────────
        existing_biz = await db.execute(
            select(Business).where(Business.slug == "test-restaurant")
        )
        if not existing_biz.scalar_one_or_none():
            business = Business(
                name="Test Restaurant",
                slug="test-restaurant",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                business_hours={
                    "mon": {"open": "08:00", "close": "22:00"},
                    "tue": {"open": "08:00", "close": "22:00"},
                    "wed": {"open": "08:00", "close": "22:00"},
                    "thu": {"open": "08:00", "close": "22:00"},
                    "fri": {"open": "08:00", "close": "23:00"},
                    "sat": {"open": "09:00", "close": "23:00"},
                    "sun": {"open": "09:00", "close": "21:00"},
                },
                greeting_text="Welcome to Test Restaurant! 🍔 How can I help you today?",
                fallback_text="Sorry, I didn't understand that. You can say 'menu' to see our menu, or 'hours' for our business hours.",
                closed_text="We're currently closed. Our hours are Mon-Thu 8am-10pm, Fri-Sat 9am-11pm, Sun 9am-9pm.",
                delivery_enabled=True,
                delivery_fee_cents=2500,  # R25.00
                address="123 Main Street, Cape Town, 8001",
                phone="+27211234567",
            )
            db.add(business)
            await db.flush()

            # ── Owner User ───────────────────────────────────────────────
            owner = BusinessUser(
                business_id=business.id,
                role="OWNER",
                email="owner@testrestaurant.co.za",
                password_hash=hash_password("Owner123!@#"),
            )
            db.add(owner)

            # ── Manager User ─────────────────────────────────────────────
            manager = BusinessUser(
                business_id=business.id,
                role="MANAGER",
                email="manager@testrestaurant.co.za",
                password_hash=hash_password("Manager123!@#"),
            )
            db.add(manager)

            # ── Staff Users (PIN login) ──────────────────────────────────
            staff1 = BusinessUser(
                business_id=business.id,
                role="STAFF",
                staff_name="Thabo",
                pin_hash=hash_pin("1234"),
            )
            staff2 = BusinessUser(
                business_id=business.id,
                role="STAFF",
                staff_name="Nandi",
                pin_hash=hash_pin("5678"),
            )
            db.add_all([staff1, staff2])

            # ── Menu Categories ──────────────────────────────────────────
            burgers = MenuCategory(
                business_id=business.id, name="Burgers", sort_order=1,
            )
            sides = MenuCategory(
                business_id=business.id, name="Sides", sort_order=2,
            )
            drinks = MenuCategory(
                business_id=business.id, name="Drinks", sort_order=3,
            )
            db.add_all([burgers, sides, drinks])
            await db.flush()

            # ── Menu Items ───────────────────────────────────────────────
            items = [
                MenuItem(
                    business_id=business.id, category_id=burgers.id,
                    name="Classic Beef Burger", price_cents=8500,
                    description="200g beef patty, lettuce, tomato, pickles, house sauce",
                    sort_order=1,
                ),
                MenuItem(
                    business_id=business.id, category_id=burgers.id,
                    name="Chicken Burger", price_cents=7500,
                    description="Grilled chicken breast, mayo, lettuce, tomato",
                    sort_order=2,
                ),
                MenuItem(
                    business_id=business.id, category_id=burgers.id,
                    name="Veggie Burger", price_cents=7000,
                    description="Plant-based patty, avocado, sprouts, chipotle mayo",
                    sort_order=3,
                ),
                MenuItem(
                    business_id=business.id, category_id=sides.id,
                    name="Chips (Regular)", price_cents=3500,
                    description="Crispy hand-cut chips", sort_order=1,
                ),
                MenuItem(
                    business_id=business.id, category_id=sides.id,
                    name="Onion Rings", price_cents=4000,
                    description="Beer-battered onion rings", sort_order=2,
                ),
                MenuItem(
                    business_id=business.id, category_id=drinks.id,
                    name="Coke 330ml", price_cents=2500, sort_order=1,
                ),
                MenuItem(
                    business_id=business.id, category_id=drinks.id,
                    name="Water 500ml", price_cents=1500, sort_order=2,
                ),
                MenuItem(
                    business_id=business.id, category_id=drinks.id,
                    name="Milkshake", price_cents=4500,
                    description="Chocolate, vanilla, or strawberry",
                    options_json={
                        "flavors": [
                            {"name": "Chocolate", "price_cents": 0},
                            {"name": "Vanilla", "price_cents": 0},
                            {"name": "Strawberry", "price_cents": 0},
                        ]
                    },
                    sort_order=3,
                ),
            ]
            db.add_all(items)

            await db.commit()
            print(f"✓ Created test business: {business.name} (code: {business.business_code})")
            print(f"  Owner: owner@testrestaurant.co.za / Owner123!@#")
            print(f"  Manager: manager@testrestaurant.co.za / Manager123!@#")
            print(f"  Staff Thabo: {business.business_code} + PIN 1234")
            print(f"  Staff Nandi: {business.business_code} + PIN 5678")
            print(f"  Menu: {len(items)} items in {3} categories")
        else:
            print("· Test business already exists")

    await engine.dispose()
    print("\n✓ Seed complete!")


if __name__ == "__main__":
    asyncio.run(seed())
