"""
End-to-end verification of the pilot till + stocktake flow, over real HTTP
against a real Postgres.

The exact flow under test:

    staff login -> open till with an opening float -> add a menu item ->
    complete the sale -> verify the completed order -> verify till totals ->
    start and complete a stocktake -> close the till -> verify expected cash,
    actual cash and variance

Unlike the other verify_* scripts, this one drives the ASGI app through
httpx rather than calling service functions. The blocker being fixed is a
workflow blocker, not a maths blocker, so the test has to cross the same
route/auth/permission boundaries a cashier's phone does — including the
one that previously made the till look permanently closed.

A second tenant is seeded alongside the first with the OPPOSITE navigation
configuration, so the run also proves that per-tenant display flags are
per-tenant.

Runs against the throwaway container only.
"""

import asyncio
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://nextgen:testpw@localhost:55444/nextgen_test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("JWT_ADMIN_SECRET_KEY", "test-admin-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "t")
os.environ.setdefault("WHATSAPP_DEFAULT_ACCESS_TOKEN", "t")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")
sys.path.insert(0, os.path.abspath("."))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from backend.app.core.security import hash_password, hash_pin  # noqa: E402
from backend.app.inventory.units import to_milli  # noqa: E402
from shared.enums import CanonicalUnit  # noqa: E402
from shared.models.business import Business  # noqa: E402
from shared.models.inventory import Ingredient  # noqa: E402
from shared.models.menu import MenuCategory, MenuItem  # noqa: E402
from shared.models.recipe import Recipe, RecipeItem  # noqa: E402
from shared.models.user import BusinessUser  # noqa: E402

RESULTS: list[bool] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    return bool(condition)


async def seed(db: AsyncSession) -> dict:
    """Two tenants with deliberately opposite navigation configuration."""
    pilot = Business(
        name="Ciao Pizza Vause Road",
        slug=f"ciao-vause-{uuid.uuid4().hex[:8]}",
        business_code=uuid.uuid4().hex[:6].upper(),
        inventory_enabled=True,
        is_whatsapp_enabled=False,
        vat_registered=True,
        vat_rate_basis_points=1500,
        vat_inclusive_pricing=True,
        pos_cash_enabled=True,
        # Till-only tenant: Stocktake replaces Wastage, Live Orders hidden.
        staff_live_orders_tab_enabled=False,
        staff_wastage_tab_enabled=False,
        staff_stocktake_tab_enabled=True,
        stocktake_frequency="DAILY_2",
        stocktake_times_json=["06:00", "22:00"],
    )
    # Untouched tenant — must keep every default.
    other = Business(
        name="Other Restaurant",
        slug=f"other-{uuid.uuid4().hex[:8]}",
        business_code=uuid.uuid4().hex[:6].upper(),
        inventory_enabled=False,
        is_whatsapp_enabled=True,
        pos_cash_enabled=True,
    )
    db.add_all([pilot, other])
    await db.flush()

    pilot_id, other_id = pilot.id, other.id
    pilot_code, other_code = pilot.business_code, other.business_code

    cashier = BusinessUser(
        business_id=pilot_id, role="STAFF", staff_name="Thandi (cashier)",
        pin_hash=hash_pin("4821"), is_active=True,
    )
    manager = BusinessUser(
        business_id=pilot_id, role="MANAGER", staff_name="Sipho (manager)",
        email=f"manager-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Sup3rSecret!pw"), is_active=True,
    )
    other_staff = BusinessUser(
        business_id=other_id, role="STAFF", staff_name="Other cashier",
        pin_hash=hash_pin("1199"), is_active=True,
    )
    db.add_all([cashier, manager, other_staff])

    category = MenuCategory(business_id=pilot_id, name="Pizza", sort_order=1, is_active=True)
    db.add(category)
    await db.flush()
    category_id = category.id

    # R120 menu price, VAT-inclusive: the customer pays exactly R120.
    item = MenuItem(
        business_id=pilot_id, category_id=category_id, name="Margherita",
        price_cents=12000, is_active=True,
    )
    cheese = Ingredient(
        business_id=pilot_id, name="Mozzarella",
        canonical_unit=CanonicalUnit.GRAM.value, count_method="WEIGHT",
        purchase_unit_name="1kg bag", purchase_pack_milli=to_milli("1", "KG"),
        cost_per_pack_cents=9500, is_active=True,
        # R95 per 1 kg = 9.5c per gram = 9_500_000 micro-cents per gram.
        cost_per_canonical_micro_cents=9_500_000,
    )
    db.add_all([item, cheese])
    await db.flush()
    item_id, cheese_id = item.id, cheese.id

    recipe = Recipe(business_id=pilot_id, menu_item_id=item_id, yield_qty=1, is_active=True)
    db.add(recipe)
    await db.flush()
    db.add(RecipeItem(
        business_id=pilot_id, recipe_id=recipe.id, ingredient_id=cheese_id,
        quantity_milli=to_milli("150", "G"),
    ))

    await db.commit()
    return {
        "pilot_id": pilot_id, "pilot_code": pilot_code,
        "other_id": other_id, "other_code": other_code,
        "cashier_id": cashier.id, "manager_email": manager.email,
        "other_staff_id": other_staff.id,
        "item_id": item_id, "cheese_id": cheese_id,
    }


async def pin_login(client: httpx.AsyncClient, code: str, staff_id, pin: str) -> str | None:
    r = await client.post("/v1/auth/pin", json={
        "business_code": code, "staff_id": str(staff_id), "pin": pin,
    })
    if r.status_code != 200:
        print(f"    ! PIN login failed {r.status_code}: {r.text[:300]}")
        return None
    return r.json()["tokens"]["access_token"]


async def main() -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        ctx = await seed(db)

    from backend.app.main import app  # imported after env is set

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # ── 1. Staff login ────────────────────────────────────────────────
        print("\n1. Staff login")
        token = await pin_login(client, ctx["pilot_code"], ctx["cashier_id"], "4821")
        if not check("cashier signs in with a PIN", token is not None):
            return 1
        staff = {"Authorization": f"Bearer {token}"}

        # ── 2. Navigation configuration reaches the app ───────────────────
        print("\n2. Tenant navigation configuration")
        r = await client.get("/v1/business/pos/settings", headers=staff)
        check("staff can read till settings", r.status_code == 200, f"HTTP {r.status_code}")
        s = r.json()
        check("Live Orders hidden for this tenant", s["staff_live_orders_tab_enabled"] is False)
        check("Wastage tab hidden for this tenant", s["staff_wastage_tab_enabled"] is False)
        check("Stocktake tab shown for this tenant", s["staff_stocktake_tab_enabled"] is True)
        check("inventory enabled for this tenant", s["inventory_enabled"] is True)

        other_token = await pin_login(client, ctx["other_code"], ctx["other_staff_id"], "1199")
        other = {"Authorization": f"Bearer {other_token}"}
        r = await client.get("/v1/business/pos/settings", headers=other)
        o = r.json()
        check("other tenant KEEPS Live Orders", o["staff_live_orders_tab_enabled"] is True)
        check("other tenant KEEPS Wastage", o["staff_wastage_tab_enabled"] is True)
        check("other tenant does NOT get Stocktake", o["staff_stocktake_tab_enabled"] is False)

        # ── 3. Sales are blocked until the till is open ───────────────────
        print("\n3. Sales blocked while the till is closed")
        r = await client.get("/v1/business/pos/till/current", headers=staff)
        check("no till open yet", r.status_code == 200 and r.json() is None)

        order_body = {
            "items": [{"menu_item_id": str(ctx["item_id"]), "quantity": 1}],
            "order_mode": "PICKUP",
            "payment_method": "CASH",
            "cash_tendered_cents": 15000,
            "idempotency_key": uuid.uuid4().hex,
        }
        r = await client.post("/v1/business/pos/orders", headers=staff, json=order_body)
        blocked = r.status_code == 409
        check("checkout refused with 409", blocked, f"HTTP {r.status_code}")
        if blocked:
            body = r.json()
            msg = str(body.get("detail") or body)
            check("refusal names the fix", "till" in msg.lower(), msg[:90])

        # ── 4. Open the till ──────────────────────────────────────────────
        print("\n4. Open the till with an opening float")
        r = await client.post("/v1/business/pos/till/open", headers=staff,
                              json={"opening_float_cents": 50000})
        check("STAFF may open the till", r.status_code == 201, f"HTTP {r.status_code}")
        till = r.json()
        check("opening float recorded", till["opening_float_cents"] == 50000)
        check("session is OPEN", till["status"] == "OPEN")

        r = await client.post("/v1/business/pos/till/open", headers=staff,
                              json={"opening_float_cents": 10000})
        check("a second till cannot be opened", r.status_code == 409, f"HTTP {r.status_code}")

        # ── 5. Ring up a sale ─────────────────────────────────────────────
        print("\n5. Add a menu item and complete the sale")
        r = await client.post("/v1/business/pos/orders", headers=staff, json=order_body)
        check("checkout now succeeds", r.status_code == 201, f"HTTP {r.status_code}")
        if r.status_code != 201:
            print(f"    ! {r.text[:400]}")
            return 1
        order = r.json()
        check("customer pays the menu price, VAT inside",
              order["total_cents"] == 12000, f"R{order['total_cents'] / 100:.2f}")
        check("order is PAID", order.get("payment_status") == "PAID", str(order.get("payment_status")))

        # ── 6. Completed order + till totals ──────────────────────────────
        print("\n6. Verify the completed order and till totals")
        r = await client.get(f"/v1/business/orders/{order['id']}", headers=staff)
        check("completed order is readable", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            check("order total unchanged on read", r.json()["total_cents"] == 12000)

        r = await client.get("/v1/business/pos/till/current", headers=staff)
        check("till still open after the sale",
              r.status_code == 200 and r.json() is not None and r.json()["status"] == "OPEN")

        # Stock moved: 150 g of mozzarella left the shelf.
        r = await client.get("/v1/business/inventory/stock", headers=staff)
        if r.status_code == 200:
            rows = r.json()
            row = next((x for x in rows if x["ingredient_id"] == str(ctx["cheese_id"])), None)
            check("the sale consumed 150 g of mozzarella",
                  row is not None and row["on_hand_milli"] == -to_milli("150", "G"),
                  f"on hand {row['on_hand_milli'] if row else 'n/a'} milli-g")
        else:
            check("stock readable after the sale", False, f"HTTP {r.status_code}")

        # ── 7. Stocktake, started by STAFF ────────────────────────────────
        print("\n7. Start and complete a stocktake")
        r = await client.post("/v1/business/stocktake/open", headers=staff, json={})
        check("STAFF may start a count", r.status_code == 201, f"HTTP {r.status_code}")
        if r.status_code != 201:
            print(f"    ! {r.text[:400]}")
            return 1
        st = r.json()
        st_id = st["id"]
        check("a line exists for every active ingredient", len(st["lines"]) >= 1)
        check("count opens as DRAFT", st["status"] == "DRAFT")

        r = await client.post("/v1/business/stocktake/open", headers=staff, json={})
        check("a second count cannot run at once", r.status_code == 409, f"HTTP {r.status_code}")

        # Count 800 g on the shelf against an expected -150 g.
        r = await client.post(f"/v1/business/stocktake/{st_id}/counts", headers=staff,
                              json={"lines": [{"ingredient_id": str(ctx["cheese_id"]), "quantity": "800"}]})
        check("staff can submit a count", r.status_code == 200, f"HTTP {r.status_code}")

        r = await client.post(f"/v1/business/stocktake/{st_id}/complete", headers=staff, json={})
        check("count completes", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            done = r.json()
            check("count is COMPLETED", done["status"] == "COMPLETED")
            line = next((l for l in done["lines"] if l["ingredient_id"] == str(ctx["cheese_id"])), None)
            check("expected quantity is snapshotted",
                  line is not None and line["expected_quantity_milli"] == -to_milli("150", "G"),
                  f"expected {line['expected_quantity_milli'] if line else 'n/a'}")
            check("counted quantity is recorded",
                  line is not None and line["counted_quantity_milli"] == to_milli("800", "G"))
            check("variance is counted minus expected",
                  line is not None
                  and line["variance_quantity_milli"]
                  == line["counted_quantity_milli"] - line["expected_quantity_milli"],
                  f"{line['variance_display'] if line else 'n/a'}")

        # ── 8. History: auditable, never silently overwritten ─────────────
        print("\n8. Stocktake history")
        r = await client.get("/v1/business/stocktake", headers=staff)
        check("history is readable", r.status_code == 200, f"HTTP {r.status_code}")
        hist = r.json() if r.status_code == 200 else []
        check("the count appears in history", len(hist) == 1, f"{len(hist)} rows")
        if hist:
            h = hist[0]
            for field in ("opened_at", "cutoff_at", "opened_by_name", "line_count",
                          "counted_line_count", "total_variance_value_cents", "notes"):
                check(f"history carries {field}", field in h)
            check("history names the staff member",
                  h["opened_by_name"] == "Thandi (cashier)", str(h["opened_by_name"]))

        # A second count must ADD a record, never replace the first.
        r = await client.post("/v1/business/stocktake/open", headers=staff, json={})
        check("a new count can start once the last is submitted",
              r.status_code == 201, f"HTTP {r.status_code}")
        r = await client.get("/v1/business/stocktake", headers=staff)
        check("history now has two separate counts",
              r.status_code == 200 and len(r.json()) == 2,
              f"{len(r.json()) if r.status_code == 200 else 'n/a'} rows")
        check("the earlier count is still COMPLETED, not overwritten",
              any(x["id"] == st_id and x["status"] == "COMPLETED" for x in r.json()))

        # ── 9. Close the till ─────────────────────────────────────────────
        print("\n9. Close the till and reconcile")
        r = await client.post(f"/v1/business/pos/till/{till['id']}/close", headers=staff,
                              json={"counted_cash_cents": 62000})
        check("a cashier may NOT close their own till", r.status_code == 403, f"HTTP {r.status_code}")

        r = await client.post("/v1/auth/login", json={
            "email": ctx["manager_email"], "password": "Sup3rSecret!pw",
        })
        check("manager signs in", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"    ! {r.text[:300]}")
            return 1
        mgr = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}

        # Float R500 + one R120 cash sale = R620 expected. Count R615: R5 short.
        r = await client.post(f"/v1/business/pos/till/{till['id']}/close", headers=mgr,
                              json={"counted_cash_cents": 61500})
        check("manager closes the till", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            closed = r.json()
            check("expected cash = float + cash sales",
                  closed["expected_cash_cents"] == 62000,
                  f"R{closed['expected_cash_cents'] / 100:.2f}")
            check("actual cash is what was counted",
                  closed["counted_cash_cents"] == 61500,
                  f"R{closed['counted_cash_cents'] / 100:.2f}")
            check("variance = actual - expected",
                  closed["variance_cents"] == 61500 - 62000,
                  f"R{closed['variance_cents'] / 100:.2f}")
            check("session is CLOSED", closed["status"] == "CLOSED")

        r = await client.post(f"/v1/business/pos/till/{till['id']}/close", headers=mgr,
                              json={"counted_cash_cents": 1})
        check("a closed till cannot be reclosed", r.status_code == 422, f"HTTP {r.status_code}")

        # ── 10. Till history for the owner/manager ────────────────────────
        print("\n10. Till history")
        r = await client.get("/v1/business/pos/till/sessions", headers=staff)
        check("a cashier may NOT browse cash-up history", r.status_code == 403, f"HTTP {r.status_code}")

        r = await client.get("/v1/business/pos/till/sessions", headers=mgr)
        check("manager can read till history", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            rows = r.json()
            check("the closed session is preserved", len(rows) == 1, f"{len(rows)} rows")
            if rows:
                h = rows[0]
                check("history names who opened it",
                      h["opened_by_name"] == "Thandi (cashier)", str(h["opened_by_name"]))
                check("history names who closed it",
                      h["closed_by_name"] == "Sipho (manager)", str(h["closed_by_name"]))
                check("history keeps expected/actual/variance",
                      h["expected_cash_cents"] == 62000
                      and h["counted_cash_cents"] == 61500
                      and h["variance_cents"] == -500)

        # ── 11. Tenant isolation on the new endpoints ─────────────────────
        print("\n11. Tenant isolation")
        r = await client.get("/v1/business/stocktake", headers=other)
        check("the other tenant sees none of this tenant's counts",
              r.status_code in (200, 409) and (r.status_code == 409 or r.json() == []),
              f"HTTP {r.status_code}")
        r = await client.get(f"/v1/business/stocktake/{st_id}", headers=other)
        check("the other tenant cannot open this count by id",
              r.status_code in (403, 404, 409), f"HTTP {r.status_code}")

        # ── 12. Schedule configuration ────────────────────────────────────
        print("\n12. Stocktake schedule")
        r = await client.get("/v1/business/settings", headers=mgr)
        check("settings readable by manager", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            b = r.json()
            check("seeded frequency is returned", b["stocktake_frequency"] == "DAILY_2")
            check("seeded times are returned", b["stocktake_times_json"] == ["06:00", "22:00"])

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"stocktake_frequency": "DAILY_4", "stocktake_times_json": None})
        check("frequency can be changed to 4x daily",
              r.status_code == 200 and r.json()["stocktake_frequency"] == "DAILY_4",
              f"HTTP {r.status_code}")

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"stocktake_frequency": "CUSTOM", "stocktake_times_json": None})
        check("CUSTOM with no times is refused", r.status_code == 422, f"HTTP {r.status_code}")

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"stocktake_frequency": "CUSTOM",
                                   "stocktake_times_json": ["22:15", "06:30"]})
        check("CUSTOM with times is accepted and sorted",
              r.status_code == 200 and r.json()["stocktake_times_json"] == ["06:30", "22:15"],
              f"HTTP {r.status_code}")

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"stocktake_frequency": "HOURLY"})
        check("an unknown frequency is rejected", r.status_code == 422, f"HTTP {r.status_code}")

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"stocktake_times_json": ["25:00"]})
        check("an impossible time is rejected", r.status_code == 422, f"HTTP {r.status_code}")

        r = await client.put("/v1/business/settings", headers=mgr,
                             json={"inventory_enabled": False})
        still_on = (await client.get("/v1/business/settings", headers=mgr)).json()["inventory_enabled"]
        check("a tenant cannot switch the inventory module on/off itself",
              still_on is True, f"inventory_enabled={still_on}")

    await engine.dispose()

    passed = sum(RESULTS)
    print(f"\n{'=' * 62}\n  {passed}/{len(RESULTS)} checks passed\n{'=' * 62}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
