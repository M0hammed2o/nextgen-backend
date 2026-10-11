"""
TEST-ONLY seed script for the split-payments + WhatsApp Pay-by-Link browser
release gate. Creates ONE dedicated, deterministic business ("NextGen Split
Payment E2E") separate from the shared Barn Owl demo tenant other e2e-tests
suites depend on, so this release gate's writes can never collide with
theirs.

Idempotent: every entity is looked up by a stable natural key before being
created, same convention as scripts/seed.py.

Run: python -m scripts.seed_split_payments_e2e
Requires DATABASE_URL to point at a reachable Postgres with migration 0055
already applied.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWNER_EMAIL = "owner@splitpay-e2e.co.za"
OWNER_PASSWORD = "SplitPayE2E2026!Owner"
MANAGER_EMAIL = "manager@splitpay-e2e.co.za"
MANAGER_PASSWORD = "SplitPayE2E2026!Manager"
CASHIER_NAME = "E2E Cashier"
CASHIER_PIN = "9999"

# Reserved sentinel for the TEMPLATE_REQUIRED scenario — matched by
# meta_mock_server.py.
LOYALTY_CUSTOMER_PHONE = "0821110099"
TEMPLATE_REQUIRED_TEST_PHONE = "0600000199"  # normalizes to +27600000199


async def seed() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core.config import get_settings
    from backend.app.core.security import hash_password, hash_pin
    from backend.app.inventory.ledger import build_movement, opening_balance_idempotency_key
    from shared.enums import StockMovementType, StockSourceType
    from shared.models import (
        Business,
        BusinessUser,
        Customer,
        LoyaltyProgram,
        LoyaltyReward,
        MenuCategory,
        MenuItem,
    )
    from shared.models.inventory import Ingredient, StockMovement
    from shared.models.recipe import Recipe, RecipeItem
    from shared.utils import generate_business_code

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        biz_result = await db.execute(select(Business).where(Business.slug == "split-payment-e2e"))
        business = biz_result.scalar_one_or_none()
        if not business:
            business = Business(
                name="NextGen Split Payment E2E",
                slug="split-payment-e2e",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                currency="ZAR",
                address="1 E2E Test Street, Cape Town, 8001",
                phone="+27210000199",
                delivery_enabled=False,
                delivery_fee_cents=0,
                max_staff_discount_cents=0,
                pos_table_number_required=False,
                # Payment methods — all enabled so Split/Pay-by-Link can show.
                pos_cash_enabled=True,
                pos_card_enabled=True,
                pos_other_payment_enabled=True,
                pos_yoco_enabled=True,
                pos_payfast_enabled=False,
                pos_ikhoka_enabled=False,
                pos_direct_eft_enabled=False,
                payment_provider="MOCK",
                payment_methods_enabled=["PAYMENT_LINK"],
                # Yoco credentials — a plain non-empty string is enough:
                # CREDENTIALS_ENCRYPTION_KEY is unset in local dev, so
                # encrypt_credential() stores this as plaintext and
                # decrypt_credential() passes it straight through. The
                # outbound Yoco HTTP call itself is mocked at the httpx
                # layer for this E2E run — see docs/SPLIT_PAYMENTS_CONTRACT.md.
                payment_api_key="sk_test_e2e_mock_yoco_key",
                payment_webhook_secret="e2e_mock_webhook_secret_v1",
                # WhatsApp — platform-token fallback path (no WhatsAppConnection
                # row created for this business), routed to the local Meta
                # mock server via META_API_BASE_URL at process launch.
                whatsapp_phone_number_id="27210000199",
                vat_registered=False,
                receipt_paper_width_mm=80,
                receipt_auto_print=True,
                receipt_copies=1,
                inventory_enabled=True,
                digital_receipts_enabled=True,
                loyalty_enabled=True,
                billing_status="TRIAL",
                plan="STARTER",
            )
            db.add(business)
            await db.flush()
            print(f"[created] Business: {business.name} (code: {business.business_code})")
        else:
            print(f"[exists]  Business already present (code: {business.business_code})")
        await db.commit()

        async def ensure_staff(email, staff_name, role, password, pin):
            query = select(BusinessUser).where(BusinessUser.business_id == business.id, BusinessUser.role == role)
            query = query.where(BusinessUser.email == email) if email else query.where(BusinessUser.staff_name == staff_name)
            existing = (await db.execute(query)).scalar_one_or_none()
            if existing:
                print(f"[exists]  {role}: {email or staff_name}")
                return existing
            user = BusinessUser(
                business_id=business.id, role=role, staff_name=staff_name, email=email,
                password_hash=hash_password(password) if password else None,
                pin_hash=hash_pin(pin) if pin else None,
                pin_updated_at=datetime.now(timezone.utc) if pin else None,
                is_active=True, must_change_password=False,
            )
            db.add(user)
            print(f"[created] {role}: {email or staff_name}")
            return user

        await ensure_staff(OWNER_EMAIL, "E2E Owner", "OWNER", OWNER_PASSWORD, None)
        await ensure_staff(MANAGER_EMAIL, "E2E Manager", "MANAGER", MANAGER_PASSWORD, None)
        await ensure_staff(None, CASHIER_NAME, "STAFF", None, CASHIER_PIN)
        await db.commit()

        async def ensure_category(name, sort_order):
            existing = (await db.execute(
                select(MenuCategory).where(MenuCategory.business_id == business.id, MenuCategory.name == name)
            )).scalar_one_or_none()
            if existing:
                return existing
            cat = MenuCategory(business_id=business.id, name=name, sort_order=sort_order)
            db.add(cat)
            await db.flush()
            print(f"[created] Category: {name}")
            return cat

        mains_cat = await ensure_category("Mains", 0)

        async def ensure_item(category, name, price_cents, sort_order=0):
            existing = (await db.execute(
                select(MenuItem).where(MenuItem.business_id == business.id, MenuItem.name == name)
            )).scalar_one_or_none()
            if existing:
                return existing
            item = MenuItem(
                business_id=business.id, category_id=category.id, name=name,
                price_cents=price_cents, sort_order=sort_order,
            )
            db.add(item)
            await db.flush()
            print(f"[created] Menu item: {name} (R{price_cents / 100:.2f})")
            return item

        burger = await ensure_item(mains_cat, "E2E Burger", 12000, 0)
        await ensure_item(mains_cat, "E2E Fries", 5000, 1)
        await ensure_item(mains_cat, "E2E Drink", 3000, 2)
        await db.commit()

        # ── Inventory: one ingredient + recipe for the Burger, so
        #    inventory-exactly-once (release gate Scenario 23) has a known,
        #    never-exhausted quantity to check before/after a sale. ───────
        ing_result = await db.execute(
            select(Ingredient).where(Ingredient.business_id == business.id, Ingredient.name == "E2E Beef Patty")
        )
        patty = ing_result.scalar_one_or_none()
        if not patty:
            patty = Ingredient(
                business_id=business.id, name="E2E Beef Patty",
                canonical_unit="UNIT", count_method="UNIT_COUNT", unit_label="patty",
                purchase_pack_milli=1000, cost_per_pack_cents=5000,
                cost_per_canonical_micro_cents=5_000_000,
            )
            db.add(patty)
            await db.flush()
            print("[created] Ingredient: E2E Beef Patty")
        await db.commit()

        recipe_result = await db.execute(select(Recipe).where(Recipe.menu_item_id == burger.id))
        recipe = recipe_result.scalar_one_or_none()
        if not recipe:
            recipe = Recipe(business_id=business.id, menu_item_id=burger.id, version=1, is_active=True, yield_qty=1)
            db.add(recipe)
            await db.flush()
            db.add(RecipeItem(
                business_id=business.id, recipe_id=recipe.id, ingredient_id=patty.id,
                quantity_milli=1000,  # 1 patty per burger sold
            ))
            print("[created] Recipe: E2E Burger -> 1x E2E Beef Patty")
        await db.commit()

        already_opened = (await db.execute(
            select(StockMovement.id).where(
                StockMovement.business_id == business.id,
                StockMovement.ingredient_id == patty.id,
                StockMovement.movement_type == StockMovementType.OPENING_BALANCE.value,
            ).limit(1)
        )).scalar_one_or_none()
        if not already_opened:
            movement = build_movement(
                business_id=business.id, ingredient_id=patty.id,
                quantity_milli=1_000_000,  # 1000 patties — never exhausted by this gate's sales
                movement_type=StockMovementType.OPENING_BALANCE.value,
                unit_cost_micro_cents=int(patty.cost_per_canonical_micro_cents or 0),
                source_type=StockSourceType.OPENING_STOCK.value, source_id=patty.id,
                idempotency_key=opening_balance_idempotency_key(patty.id),
                notes="E2E release-gate opening stock",
            )
            db.add(movement)
            print("[created] Opening stock: 1000 x E2E Beef Patty")
        await db.commit()

        # ── Loyalty: programme + reward + one customer with a known balance ─
        existing_program = (await db.execute(
            select(LoyaltyProgram).where(LoyaltyProgram.business_id == business.id)
        )).scalar_one_or_none()
        if not existing_program:
            db.add(LoyaltyProgram(
                business_id=business.id, is_enabled=True,
                points_per_rand=1, points_per_order=0,
                points_expiry_days=365, min_redemption_points=50,
                silver_tier_points=500, gold_tier_points=2000,
            ))
            print("[created] Loyalty programme")
        await db.commit()

        existing_reward = (await db.execute(
            select(LoyaltyReward).where(LoyaltyReward.business_id == business.id, LoyaltyReward.name == "E2E R50 Off")
        )).scalar_one_or_none()
        if not existing_reward:
            db.add(LoyaltyReward(
                business_id=business.id, name="E2E R50 Off",
                description="R50 off for the split-payment + loyalty release-gate scenario.",
                points_cost=50, value_cents=5000, is_active=True,
            ))
            print("[created] Loyalty reward: E2E R50 Off (50 pts, R50)")
        await db.commit()

        existing_customer = (await db.execute(
            select(Customer).where(Customer.business_id == business.id, Customer.phone_number == LOYALTY_CUSTOMER_PHONE)
        )).scalar_one_or_none()
        if not existing_customer:
            db.add(Customer(
                business_id=business.id, phone_number=LOYALTY_CUSTOMER_PHONE,
                display_name="E2E Loyalty Customer", loyalty_points_balance=500,
            ))
            print(f"[created] Loyalty customer: E2E Loyalty Customer ({LOYALTY_CUSTOMER_PHONE}, 500 pts)")
        await db.commit()

    await engine.dispose()
    print("\nSeed complete.")
    print(f"Business code: {business.business_code}")
    print(f"Owner: {OWNER_EMAIL} / {OWNER_PASSWORD}")
    print(f"Manager: {MANAGER_EMAIL} / {MANAGER_PASSWORD}")
    print(f"Cashier: {CASHIER_NAME} / PIN {CASHIER_PIN}")


if __name__ == "__main__":
    asyncio.run(seed())
