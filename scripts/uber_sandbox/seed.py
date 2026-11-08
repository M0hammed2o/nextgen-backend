"""Seed the two Uber sandbox test tenants into the SANDBOX DB (idempotent).

One NextGen business per Uber test store, each with an OWNER login, a STAFF PIN,
and a SANDBOX/sandbox Uber Eats connection holding that store's id (read from the
git-ignored .env.uber-sandbox-stores). Logins go to logins.local.txt next to
this file (outside the repo), never to stdout. From the backend worktree root:
    python <this>
"""
import asyncio
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack  # noqa: E402

stack.load()
sys.path.insert(0, os.getcwd())

from sqlalchemy import select  # noqa: E402

from backend.app.core.security import hash_password, hash_pin  # noqa: E402
from backend.app.db.session import async_session_factory  # noqa: E402
from shared.models.business import Business  # noqa: E402
from shared.models.delivery import DeliveryConnection  # noqa: E402
from shared.models.user import BusinessUser  # noqa: E402
from shared.utils import generate_business_code  # noqa: E402

stores = stack._read(".env.uber-sandbox-stores")
TENANTS = [
    ("NextGen Intelligence Test Restaurant 1", "uber-sandbox-test-1", stores["UBER_SANDBOX_STORE_1_ID"]),
    ("NextGen Intelligence Test Restaurant 2", "uber-sandbox-test-2", stores["UBER_SANDBOX_STORE_2_ID"]),
]


async def main() -> None:
    out, logins = [], []
    async with async_session_factory() as db:
        for name, slug, store_id in TENANTS:
            business = (await db.execute(select(Business).where(Business.slug == slug))).scalar_one_or_none()
            if business is None:
                business = Business(
                    name=f"{name} (Uber sandbox)", slug=slug, business_code=generate_business_code(),
                    timezone="Africa/Johannesburg", currency="ZAR", is_active=True,
                    delivery_platforms_enabled=True, uber_eats_enabled=True, pos_enabled=True, inventory_enabled=True,
                    billing_status="TRIAL", trial_ends_at=datetime.now(timezone.utc) + timedelta(days=60),
                    address="34 Peter Mokaba Road, Musgrave, Durban, 4001 (Uber sandbox test store)",
                )
                db.add(business)
                await db.flush()
                password, pin = secrets.token_urlsafe(12), f"{secrets.randbelow(10**6):06d}"
                email = f"owner+{slug}@example.com"
                db.add(BusinessUser(business_id=business.id, role="OWNER", staff_name="Sandbox Owner", email=email,
                                    password_hash=hash_password(password), is_active=True, must_change_password=False))
                db.add(BusinessUser(business_id=business.id, role="STAFF", staff_name="Sandbox Kitchen",
                                    pin_hash=hash_pin(pin), pin_updated_at=datetime.now(timezone.utc), is_active=True,
                                    must_change_password=False))
                logins.append(f"{name}: business code {business.business_code} | owner {email} / {password} | staff PIN {pin}")
            connection = (await db.execute(select(DeliveryConnection).where(
                DeliveryConnection.business_id == business.id, DeliveryConnection.provider == "UBER_EATS",
            ))).scalar_one_or_none()
            if connection is None:
                db.add(DeliveryConnection(
                    business_id=business.id, provider="UBER_EATS", external_store_id=store_id,
                    status="SANDBOX", environment="sandbox", auto_accept_enabled=False,
                ))
            out.append({"name": name, "business_id": str(business.id), "store_id": store_id})
        await db.commit()

    json.dump(out, open(stack.TENANTS, "w", encoding="utf-8"), indent=2)
    if logins:
        with open(stack.LOGINS, "a", encoding="utf-8") as fh:
            fh.write("\n".join(logins) + "\n")
    env = stack._read(stack.ENV_FILE)
    if "UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS" not in env:
        with open(stack.ENV_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS={','.join(t['business_id'] for t in out)}\n")
    for t in out:
        print(f"{t['name']}: business {t['business_id']} <- Uber store {t['store_id'][:4]}…{t['store_id'][-4:]}")
    print(f"Logins written to {stack.LOGINS} ({len(logins)} new; not shown)")


asyncio.run(main())
