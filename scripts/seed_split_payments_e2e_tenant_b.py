"""
TEST-ONLY second business for the split-payments release gate's
cross-tenant-attack scenarios (Scenario 12). Minimal — only what's needed
to authenticate as a different tenant and attempt to touch Business A's
payment-link intents. See seed_split_payments_e2e.py for the primary
business; this one deliberately does NOT share any menu/inventory/loyalty
setup since none of that is exercised here.

Run: python -m scripts.seed_split_payments_e2e_tenant_b
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWNER_EMAIL = "owner@splitpay-e2e-b.co.za"
OWNER_PASSWORD = "SplitPayE2EB2026!Owner"
CASHIER_NAME = "E2E Cashier B"
CASHIER_PIN = "8888"


async def seed() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.core.config import get_settings
    from backend.app.core.security import hash_password, hash_pin
    from shared.models import Business, BusinessUser
    from shared.utils import generate_business_code

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        biz_result = await db.execute(select(Business).where(Business.slug == "split-payment-e2e-tenant-b"))
        business = biz_result.scalar_one_or_none()
        if not business:
            business = Business(
                name="NextGen Split Payment E2E — Tenant B",
                slug="split-payment-e2e-tenant-b",
                business_code=generate_business_code(),
                timezone="Africa/Johannesburg", currency="ZAR",
                address="2 E2E Test Street, Cape Town, 8001", phone="+27210000299",
                delivery_enabled=False, delivery_fee_cents=0, max_staff_discount_cents=0,
                pos_table_number_required=False,
                pos_cash_enabled=True, pos_card_enabled=True, pos_other_payment_enabled=True,
                pos_yoco_enabled=True, pos_payfast_enabled=False,
                payment_provider="MOCK", payment_methods_enabled=["PAYMENT_LINK"],
                payment_api_key="sk_test_e2e_mock_yoco_key_tenant_b",
                payment_webhook_secret="e2e_mock_webhook_secret_v1_tenant_b",
                whatsapp_phone_number_id="27210000299",
                vat_registered=False, inventory_enabled=False,
                digital_receipts_enabled=True, loyalty_enabled=False,
                billing_status="TRIAL", plan="STARTER",
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

        await ensure_staff(OWNER_EMAIL, "E2E Owner B", "OWNER", OWNER_PASSWORD, None)
        await ensure_staff(None, CASHIER_NAME, "STAFF", None, CASHIER_PIN)
        await db.commit()

    await engine.dispose()
    print("\nSeed complete.")
    print(f"Business code: {business.business_code}")
    print(f"Owner: {OWNER_EMAIL} / {OWNER_PASSWORD}")
    print(f"Cashier: {CASHIER_NAME} / PIN {CASHIER_PIN}")


if __name__ == "__main__":
    asyncio.run(seed())
