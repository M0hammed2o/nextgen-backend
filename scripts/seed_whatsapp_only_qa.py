"""
TEST-ONLY seed script for manual/browser QA of the WhatsApp-only subscription
package (feat/whatsapp-only-subscription). NOT part of the pytest suite.

Creates:
  - A WhatsApp-only tenant: pos_enabled=False, is_whatsapp_enabled=True,
    whatsapp_order_allowance=1000, with menu/customers/orders across statuses,
    an OWNER/MANAGER login, and a STAFF PIN login. Also bulk-inserts enough
    terminal-status WHATSAPP orders to push usage to >=80% of the allowance
    so the usage-alert / usage-dashboard screens have real data to show.
  - A regression tenant: pos_enabled=True (default), with POS/till order
    history, an OWNER/MANAGER login, and a closed till session.
  - A SUPER_ADMIN AdminUser for the Admin Suite.

Idempotent: every entity is looked up by a stable natural key before being
created, same convention as scripts/seed_split_payments_e2e.py.

Run against the DISPOSABLE docker-compose.test.yml stack only:
  DATABASE_URL="postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test" \
    python -m scripts.seed_whatsapp_only_qa
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WA_OWNER_EMAIL = "owner@wa-only-qa.co.za"
WA_OWNER_PASSWORD = "WaOnlyQA2026!Owner"
WA_MANAGER_EMAIL = "manager@wa-only-qa.co.za"
WA_MANAGER_PASSWORD = "WaOnlyQA2026!Manager"
WA_STAFF_NAME = "WA QA Staff"
WA_STAFF_PIN = "4321"

POS_OWNER_EMAIL = "owner@pos-regression-qa.co.za"
POS_OWNER_PASSWORD = "PosRegQA2026!Owner"
POS_STAFF_NAME = "POS QA Staff"
POS_STAFF_PIN = "1234"

SUPER_ADMIN_EMAIL = "superadmin@nextgen-qa.co.za"
SUPER_ADMIN_PASSWORD = "SuperAdminQA2026!"


async def seed() -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core.config import get_settings
    from backend.app.core.security import hash_password, hash_pin
    from shared.enums import OrderSource, OrderStatus
    from shared.models import Business, BusinessUser, Customer, MenuCategory, MenuItem
    from shared.models.admin import AdminUser
    from shared.models.analytics import DailyUsage
    from shared.models.order import Order, OrderItem
    from shared.models.till import TillSession
    from shared.models.usage_alert import UsageAlert
    from shared.utils import generate_business_code

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # ================= WhatsApp-only tenant =================
        biz_result = await db.execute(select(Business).where(Business.slug == "wa-only-qa"))
        wa_business = biz_result.scalar_one_or_none()
        if not wa_business:
            wa_business = Business(
                name="QA WhatsApp Only Bistro",
                slug="wa-only-qa",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                currency="ZAR",
                address="1 QA Test Street, Cape Town, 8001",
                phone="+27210000299",
                delivery_enabled=False,
                delivery_fee_cents=0,
                max_staff_discount_cents=0,
                pos_cash_enabled=True,
                pos_card_enabled=True,
                payment_provider="MOCK",
                whatsapp_phone_number_id="27210000299",
                is_whatsapp_enabled=True,
                vat_registered=False,
                receipt_paper_width_mm=80,
                billing_status="TRIAL",
                plan="STARTER",
                # ── The feature under test ──
                pos_enabled=False,
                whatsapp_order_allowance=1000,
            )
            db.add(wa_business)
            await db.flush()
            print(f"[created] WA-only Business: {wa_business.name} (code: {wa_business.business_code})")
        else:
            wa_business.pos_enabled = False
            wa_business.is_whatsapp_enabled = True
            wa_business.whatsapp_order_allowance = 1000
            print(f"[exists]  WA-only Business already present (code: {wa_business.business_code})")
        await db.commit()

        async def ensure_staff(business, email, staff_name, role, password, pin):
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

        await ensure_staff(wa_business, WA_OWNER_EMAIL, "WA QA Owner", "OWNER", WA_OWNER_PASSWORD, None)
        await ensure_staff(wa_business, WA_MANAGER_EMAIL, "WA QA Manager", "MANAGER", WA_MANAGER_PASSWORD, None)
        await ensure_staff(wa_business, None, WA_STAFF_NAME, "STAFF", None, WA_STAFF_PIN)
        await db.commit()

        async def ensure_category(business, name, sort_order):
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

        async def ensure_item(business, category, name, price_cents, sort_order=0):
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

        wa_mains = await ensure_category(wa_business, "Mains", 0)
        wa_drinks = await ensure_category(wa_business, "Drinks", 1)
        wa_burger = await ensure_item(wa_business, wa_mains, "QA WA Burger", 8500, 0)
        wa_pizza = await ensure_item(wa_business, wa_mains, "QA WA Pizza", 11000, 1)
        wa_coke = await ensure_item(wa_business, wa_drinks, "QA WA Coke", 2500, 0)
        await db.commit()

        async def ensure_customer(business, wa_id, display_name, phone):
            existing = (await db.execute(
                select(Customer).where(Customer.business_id == business.id, Customer.wa_id == wa_id)
            )).scalar_one_or_none()
            if existing:
                return existing
            cust = Customer(
                business_id=business.id, wa_id=wa_id, display_name=display_name,
                phone_number=phone, last_message_at=datetime.now(timezone.utc),
            )
            db.add(cust)
            await db.flush()
            print(f"[created] Customer: {display_name} ({wa_id})")
            return cust

        wa_customers = [
            await ensure_customer(wa_business, "27820001001", "Thandi Nkosi", "+27820001001"),
            await ensure_customer(wa_business, "27820001002", "Sipho Dlamini", "+27820001002"),
            await ensure_customer(wa_business, "27820001003", "Lerato Mokoena", "+27820001003"),
        ]
        await db.commit()

        async def ensure_order(business, customer, order_number, status, source, items, days_ago=0):
            existing = (await db.execute(
                select(Order).where(Order.business_id == business.id, Order.order_number == order_number)
            )).scalar_one_or_none()
            if existing:
                return existing
            subtotal = sum(p * q for _, p, q in items)
            order = Order(
                business_id=business.id,
                customer_id=customer.id if customer else None,
                order_number=order_number,
                status=status,
                order_mode="PICKUP",
                source=source,
                subtotal_cents=subtotal,
                total_cents=subtotal,
                customer_name=customer.display_name if customer else None,
                phone_number=customer.phone_number if customer else None,
                created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
            )
            db.add(order)
            await db.flush()
            for name, price, qty in items:
                db.add(OrderItem(
                    order_id=order.id, business_id=business.id,
                    name_snapshot=name, base_price_cents=price, option_adjustment_cents=0,
                    add_on_total_cents=0, unit_price_cents=price, quantity=qty,
                    line_total_cents=price * qty,
                ))
            print(f"[created] Order {order_number} [{status}/{source}]")
            return order

        wa_order_defs = [
            ("QAWA-0001", OrderStatus.NEW.value, wa_customers[0], [(wa_burger.name, 8500, 1), (wa_coke.name, 2500, 1)]),
            ("QAWA-0002", OrderStatus.ACCEPTED.value, wa_customers[1], [(wa_pizza.name, 11000, 1)]),
            ("QAWA-0003", OrderStatus.COLLECTED.value, wa_customers[2], [(wa_burger.name, 8500, 2)]),
            ("QAWA-0004", OrderStatus.CANCELLED.value, wa_customers[0], [(wa_pizza.name, 11000, 1)]),
            ("QAWA-0005", OrderStatus.IN_PROGRESS.value, wa_customers[1], [(wa_coke.name, 2500, 3)]),
        ]
        for i, (num, status, cust, items) in enumerate(wa_order_defs):
            await ensure_order(wa_business, cust, num, status, OrderSource.WHATSAPP.value, items, days_ago=i)
        await db.commit()

        # ── Bulk-insert enough billable WHATSAPP orders to cross 80% of the
        #    1000 allowance (need >=800 COLLECTED-since-period-start orders).
        #    No items needed -- only Order.status/source/created_at matter
        #    for backend/app/billing/whatsapp_usage.py's count query.
        count_result = await db.execute(
            select(func.count()).select_from(Order).where(
                Order.business_id == wa_business.id,
                Order.source == OrderSource.WHATSAPP.value,
                Order.status.in_([OrderStatus.COLLECTED.value, OrderStatus.DELIVERED.value, OrderStatus.COMPLETED.value]),
                Order.created_at >= wa_business.billing_period_started_at,
            )
        )
        already_billable = int(count_result.scalar_one() or 0)
        target = 850  # 85% of 1000 -- comfortably over the 80% alert threshold
        to_create = max(0, target - already_billable)
        if to_create:
            start_seq = 100000
            for i in range(to_create):
                db.add(Order(
                    business_id=wa_business.id,
                    order_number=f"QAWA-BULK-{start_seq + i}",
                    status=OrderStatus.COLLECTED.value,
                    order_mode="PICKUP",
                    source=OrderSource.WHATSAPP.value,
                    subtotal_cents=5000,
                    total_cents=5000,
                    customer_name="Bulk QA Customer",
                    phone_number="+27820009999",
                ))
            await db.commit()
            print(f"[created] {to_create} bulk billable WHATSAPP orders (usage now ~{already_billable + to_create}/1000)")
        else:
            print(f"[exists]  Already {already_billable} billable WHATSAPP orders this period")

        final_billable = already_billable + to_create

        # ── DailyUsage rows: admin suite's /usage (per-business usage/AI-cost
        #    dashboard) reads GET /admin/usage, which aggregates this table --
        #    not the orders table -- so it would show all-zero rows for a
        #    freshly-created business otherwise.
        today = datetime.now(timezone.utc).date()
        for i in range(5):
            day = today - timedelta(days=i)
            existing_usage = (await db.execute(
                select(DailyUsage).where(DailyUsage.business_id == wa_business.id, DailyUsage.day == day)
            )).scalar_one_or_none()
            if existing_usage:
                continue
            db.add(DailyUsage(
                business_id=wa_business.id, day=day,
                inbound_messages=40 + i * 5, outbound_messages=40 + i * 5,
                llm_calls=35 + i * 4, llm_tokens=12000 + i * 1500, llm_cost_cents=180 + i * 20,
                orders_created=10 + i, orders_completed=8 + i, cancelled_orders=1,
                revenue_cents=(8 + i) * 8500,
            ))
        await db.commit()
        print("[created] DailyUsage rows (last 5 days) for WA-only tenant")

        # ── UsageAlert row: the admin suite DASHBOARD's usage-alerts widget
        #    (GET /admin/usage-alerts) reads ONLY this table -- it is written
        #    by the cron job in backend/app/worker.py (never touched by this
        #    QA session, per the task's protected-file list), so without a
        #    directly-seeded row here the widget would show "no businesses
        #    have crossed a threshold" even though usage is really at 85%.
        existing_alert = (await db.execute(
            select(UsageAlert).where(
                UsageAlert.business_id == wa_business.id,
                UsageAlert.billing_period_started_at == wa_business.billing_period_started_at,
                UsageAlert.threshold_crossed == 80,
            )
        )).scalar_one_or_none()
        if not existing_alert:
            db.add(UsageAlert(
                business_id=wa_business.id,
                billing_period_started_at=wa_business.billing_period_started_at,
                threshold_crossed=80,
                orders_used=final_billable,
                orders_allowance=wa_business.whatsapp_order_allowance,
            ))
            await db.commit()
            print(f"[created] UsageAlert: 80% threshold crossed ({final_billable}/{wa_business.whatsapp_order_allowance})")
        else:
            print("[exists]  UsageAlert 80% row already present")

        # ================= Regression (POS-enabled) tenant =================
        pos_result = await db.execute(select(Business).where(Business.slug == "pos-regression-qa"))
        pos_business = pos_result.scalar_one_or_none()
        if not pos_business:
            pos_business = Business(
                name="QA POS Regression Diner",
                slug="pos-regression-qa",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg",
                currency="ZAR",
                address="2 QA Test Street, Cape Town, 8001",
                phone="+27210000399",
                delivery_enabled=False,
                delivery_fee_cents=0,
                max_staff_discount_cents=0,
                pos_cash_enabled=True,
                pos_card_enabled=True,
                payment_provider="MOCK",
                whatsapp_phone_number_id="27210000399",
                is_whatsapp_enabled=True,
                vat_registered=False,
                receipt_paper_width_mm=80,
                billing_status="TRIAL",
                plan="STARTER",
                # pos_enabled left at its TRUE default -- regression tenant
            )
            db.add(pos_business)
            await db.flush()
            print(f"[created] Regression Business: {pos_business.name} (code: {pos_business.business_code})")
        else:
            print(f"[exists]  Regression Business already present (code: {pos_business.business_code})")
        await db.commit()

        pos_owner = await ensure_staff(pos_business, POS_OWNER_EMAIL, "POS QA Owner", "OWNER", POS_OWNER_PASSWORD, None)
        await ensure_staff(pos_business, None, POS_STAFF_NAME, "STAFF", None, POS_STAFF_PIN)
        await db.commit()

        pos_mains = await ensure_category(pos_business, "Mains", 0)
        pos_item = await ensure_item(pos_business, pos_mains, "QA POS Steak", 15000, 0)
        await ensure_item(pos_business, pos_mains, "QA POS Salad", 7000, 1)
        await db.commit()

        # POS-created customer -- no WhatsApp identity, looked up by phone
        # rather than wa_id (ensure_customer's natural key is wa_id).
        existing_pos_cust = (await db.execute(
            select(Customer).where(Customer.business_id == pos_business.id, Customer.phone_number == "+27820002001")
        )).scalar_one_or_none()
        if not existing_pos_cust:
            db.add(Customer(business_id=pos_business.id, display_name="Walk-in Regular", phone_number="+27820002001"))
            await db.commit()
            print("[created] Customer: Walk-in Regular (POS, no wa_id)")
        else:
            print("[exists]  Customer: Walk-in Regular")

        await ensure_order(pos_business, None, "QAPOS-0001", OrderStatus.COMPLETED.value, OrderSource.MANUAL.value,
                            [(pos_item.name, 15000, 1)], days_ago=1)
        await ensure_order(pos_business, None, "QAPOS-0002", OrderStatus.COMPLETED.value, OrderSource.MANUAL.value,
                            [(pos_item.name, 15000, 2)], days_ago=2)
        await db.commit()

        existing_till = (await db.execute(
            select(TillSession).where(TillSession.business_id == pos_business.id)
        )).scalar_one_or_none()
        if not existing_till:
            db.add(TillSession(
                business_id=pos_business.id,
                opened_by_user_id=pos_owner.id,
                closed_by_user_id=pos_owner.id,
                approved_by_user_id=pos_owner.id,
                opening_float_cents=50000,
                expected_cash_cents=65000,
                counted_cash_cents=65000,
                variance_cents=0,
                status="CLOSED",
                closed_at=datetime.now(timezone.utc) - timedelta(days=1),
            ))
            await db.commit()
            print("[created] Closed TillSession history for regression tenant")

        # ================= Super Admin =================
        admin_result = await db.execute(select(AdminUser).where(AdminUser.email == SUPER_ADMIN_EMAIL))
        admin_user = admin_result.scalar_one_or_none()
        if not admin_user:
            admin_user = AdminUser(
                email=SUPER_ADMIN_EMAIL,
                password_hash=hash_password(SUPER_ADMIN_PASSWORD),
                display_name="QA Super Admin",
                role="SUPER_ADMIN",
                is_active=True,
            )
            db.add(admin_user)
            await db.commit()
            print(f"[created] SUPER_ADMIN: {SUPER_ADMIN_EMAIL}")
        else:
            print(f"[exists]  SUPER_ADMIN already present: {SUPER_ADMIN_EMAIL}")

    await engine.dispose()
    print("\nSeed complete.")
    print(f"WA-only business code: {wa_business.business_code}")
    print(f"  Owner:   {WA_OWNER_EMAIL} / {WA_OWNER_PASSWORD}")
    print(f"  Manager: {WA_MANAGER_EMAIL} / {WA_MANAGER_PASSWORD}")
    print(f"  Staff:   {WA_STAFF_NAME} / PIN {WA_STAFF_PIN}")
    print(f"Regression business code: {pos_business.business_code}")
    print(f"  Owner:   {POS_OWNER_EMAIL} / {POS_OWNER_PASSWORD}")
    print(f"  Staff:   {POS_STAFF_NAME} / PIN {POS_STAFF_PIN}")
    print(f"Super Admin: {SUPER_ADMIN_EMAIL} / {SUPER_ADMIN_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed())
