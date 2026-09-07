"""
End-to-end verification of EXTERNAL SALES CHANNELS against a real Postgres.

The scenario from the brief, exactly:

    POS 20 + Uber 10 + Mr D 5 + wastage 2  ->  theoretical stock
    duplicate Uber upload                  ->  ZERO extra deduction
    stocktake                              ->  variance and rand value

Plus the things that only fail in reality: a concurrent double upload, a late
import into a closed stocktake period, external money staying out of till
cash-up, and tenant isolation.

Throwaway database only. Never point this at production.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://nextgen:testpw@localhost:55446/nextgen_test"
)
for _k in ("JWT_SECRET_KEY", "JWT_ADMIN_SECRET_KEY", "META_VERIFY_TOKEN",
           "WHATSAPP_DEFAULT_ACCESS_TOKEN"):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, os.path.abspath("."))

from sqlalchemy import func, select                                    # noqa: E402
from sqlalchemy.exc import IntegrityError                              # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from backend.app.api.v1 import routes_import, routes_stocktake         # noqa: E402
from backend.app.core.rbac import AuthUser                             # noqa: E402
from backend.app.external_orders.adapters import adapt_rows            # noqa: E402
from backend.app.external_orders.service import (                      # noqa: E402
    import_resolved_orders,
    resolve_orders,
)
from backend.app.inventory.ledger import build_movement, post_sale_consumption  # noqa: E402
from backend.app.inventory.units import to_milli                       # noqa: E402
from backend.app.pos.pricing import ResolvedPOSItem                    # noqa: E402
from shared.enums import (                                             # noqa: E402
    EXTERNAL_ORDER_SOURCES,
    ExternalMappingKind,
    ExternalPlatform,
    StockMovementType,
)
from shared.models.business import Business                            # noqa: E402
from shared.models.external_orders import ExternalProductMapping       # noqa: E402
from shared.models.inventory import Ingredient                         # noqa: E402
from shared.models.menu import MenuItem                                # noqa: E402
from shared.models.order import Order                                  # noqa: E402
from shared.pricing.engine import LineItemBreakdown, calculate_vat_cents  # noqa: E402

OK = []


def check(label, condition, detail=""):
    OK.append(bool(condition))
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


class FakeUpload:
    def __init__(self, filename, content):
        self.filename = filename
        self._data = content.encode("utf-8")

    async def read(self):
        return self._data


MENU_CSV = """category,name,description,price,size
Pizza,Margherita,Tomato and cheese,89.00,Small
Pizza,Margherita,Tomato and cheese,149.00,Large
"""

INGREDIENT_CSV = """name,unit,unit_label,category,pack_size,pack_unit,pack_cost,opening_quantity
Large Dough Portion,portion,portion,Dough,1,portion,6.50,100
Sauce Portion,portion,portion,Sauces,1,portion,3.20,100
Cheese Portion,portion,portion,Dairy,1,portion,15.00,100
Flour,g,,Dry Goods,1,kg,18.00,100000
Large Box,box,box,Packaging,1,box,4.10,100
"""

RECIPE_CSV = """menu_item,ingredient,quantity,unit,option_id
Margherita,Large Dough Portion,1,portion,size-large
Margherita,Sauce Portion,1,portion,size-large
Margherita,Cheese Portion,1,portion,size-large
Margherita,Flour,20,g,size-large
Margherita,Large Box,1,box,size-large
"""


# Sales happen two days ago; the stocktake cut-off is "now". Hardcoded
# calendar dates would sort after the cut-off on a machine whose clock is
# earlier, and the closed-period path would never be reached.
SALE_DAY = (datetime.now(timezone.utc) - timedelta(days=2)).date()
SALE_DAY_STR = SALE_DAY.isoformat()


def uber_csv(rows):
    header = "Order ID,Order Date,Order Time,Item,Qty,Modifiers,Price,Status\n"
    return header + "".join(rows)


def uber_rows(start, count, when=None, time="18:42"):
    when = when or SALE_DAY_STR
    return [
        f"UE-{start + i},{when},{time},Large Margherita Pizza,1,,149.00,Delivered\n"
        for i in range(count)
    ]


def mrd_csv(count):
    header = "Order Number,Ordered At,Product,Quantity,Gross,State\n"
    return header + "".join(
        f"MRD-{9000 + i},{SALE_DAY_STR} 19:1{i % 10},Margherita Large,1,138.00,Delivered\n"
        for i in range(count)
    )


async def cheese_expected(db, business_id, cheese_id):
    total = (
        await db.execute(
            select(func.coalesce(func.sum(
                __import__("shared.models.inventory", fromlist=["x"]).StockMovement.quantity_milli
            ), 0)).where(
                __import__("shared.models.inventory", fromlist=["x"]).StockMovement.business_id == business_id,
                __import__("shared.models.inventory", fromlist=["x"]).StockMovement.ingredient_id == cheese_id,
            )
        )
    ).scalar()
    return int(total or 0)


async def run_import(db, business, platform, csv_text, user, **kwargs):
    """Preview + commit through the same path the API uses."""
    rows = await routes_import._read_rows(FakeUpload("f.csv", csv_text))
    adapted = adapt_rows(rows, platform=platform)
    summary = await resolve_orders(
        db, business_id=business.id, platform=platform, orders=adapted.orders
    )
    outcome = await import_resolved_orders(
        db, business=business, platform=platform, batch_id=uuid.uuid4(),
        summary=summary, user_id=user.user_id, **kwargs,
    )
    await db.commit()
    return summary, outcome


async def main():
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        biz = Business(
            name="External Channel Test Pizzeria", slug=f"ext-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(), inventory_enabled=True,
            is_whatsapp_enabled=False, vat_registered=True,
            vat_rate_basis_points=1500, vat_inclusive_pricing=True,
        )
        other = Business(
            name="Other Pizzeria", slug=f"oth-{uuid.uuid4().hex[:8]}",
            business_code=uuid.uuid4().hex[:6].upper(), inventory_enabled=True,
        )
        db.add_all([biz, other]); await db.flush()
        BIZ, OTHER = biz.id, other.id
        await db.commit()

        owner = AuthUser(user_id=uuid.uuid4(), business_id=BIZ, role="OWNER", token_type="access")
        other_owner = AuthUser(user_id=uuid.uuid4(), business_id=OTHER, role="OWNER",
                               token_type="access")

        # ── Setup via self-service imports ────────────────────────────────
        print("\n== SETUP (owner self-service) ==")
        await routes_import.commit_menu(
            file=FakeUpload("m.csv", MENU_CSV), mapping=None, user=owner, db=db)
        await routes_import.commit_ingredients(
            file=FakeUpload("i.csv", INGREDIENT_CSV), mapping=None, user=owner, db=db)
        await routes_import.commit_recipes(
            file=FakeUpload("r.csv", RECIPE_CSV), mapping=None, user=owner, db=db)

        ings = {
            i.name: i for i in (
                await db.execute(select(Ingredient).where(Ingredient.business_id == BIZ))
            ).scalars().all()
        }
        CHEESE = ings["Cheese Portion"].id
        pizza = (await db.execute(
            select(MenuItem).where(MenuItem.business_id == BIZ, MenuItem.name == "Margherita")
        )).scalar_one()
        PIZZA = pizza.id
        check("menu, ingredients and recipe imported", len(ings) == 5 and pizza is not None)
        check("opening cheese = 100 portions",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(100, "portion"))

        # ── 1. In-store: sell 20 ──────────────────────────────────────────
        print("\n== IN-STORE: 20 Large Margheritas ==")
        resolved_pos = ResolvedPOSItem(
            menu_item_id=PIZZA, name="Margherita",
            breakdown=LineItemBreakdown(8900, 6000, 0, 14900, 20, 298000),
            selected_options_snapshot=[{
                "group_id": "size", "group_name": "Size",
                "option_id": "size-large", "option_name": "Large", "price_delta_cents": 6000}],
            add_ons_snapshot=[], removed_ingredients_snapshot=[],
            special_instructions=None, requires_preparation=True,
        )
        await post_sale_consumption(
            db, business_id=BIZ, order_id=uuid.uuid4(), resolved_items=[resolved_pos],
            occurred_at=datetime.combine(SALE_DAY, datetime.min.time(), tzinfo=timezone.utc)
                        + timedelta(hours=12))
        await db.commit()
        check("cheese 100 -> 80 portions",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(80, "portion"),
              str(await cheese_expected(db, BIZ, CHEESE)))

        # ── 2. Uber Eats: map the product, then import 10 ─────────────────
        print("\n== UBER EATS: 10 orders ==")
        first, _ = await run_import(
            db, biz, ExternalPlatform.UBER_EATS.value, uber_csv(uber_rows(12345, 10)), owner)
        check("unmapped product is blocked, not guessed",
              len(first.blocked) == 10 and "Large Margherita Pizza" in first.unresolved_item_names,
              str(first.unresolved_item_names))
        check("nothing was deducted while blocked",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(80, "portion"))

        db.add(ExternalProductMapping(
            business_id=BIZ, platform=ExternalPlatform.UBER_EATS.value,
            kind=ExternalMappingKind.ITEM.value,
            external_text="Large Margherita Pizza", external_key="large margherita pizza",
            menu_item_id=PIZZA, option_id="size-large", created_by_user_id=owner.user_id))
        await db.commit()

        summary, outcome = await run_import(
            db, biz, ExternalPlatform.UBER_EATS.value, uber_csv(uber_rows(12345, 10)), owner)
        check("10 Uber orders imported", outcome.orders_created == 10, str(outcome.orders_created))
        check("cheese 80 -> 70 portions",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(70, "portion"),
              str(await cheese_expected(db, BIZ, CHEESE)))

        # ── 3. Mr D: different column names AND product name ──────────────
        print("\n== MR D: 5 orders (different export format) ==")
        db.add(ExternalProductMapping(
            business_id=BIZ, platform=ExternalPlatform.MR_D.value,
            kind=ExternalMappingKind.ITEM.value,
            external_text="Margherita Large", external_key="margherita large",
            menu_item_id=PIZZA, option_id="size-large", created_by_user_id=owner.user_id))
        await db.commit()

        _, mrd_outcome = await run_import(
            db, biz, ExternalPlatform.MR_D.value, mrd_csv(5), owner)
        check("Mr D's own column headings auto-detected", mrd_outcome.orders_created == 5,
              str(mrd_outcome.orders_created))
        check("cheese 70 -> 65 portions",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(65, "portion"),
              str(await cheese_expected(db, BIZ, CHEESE)))
        check("both channels used the SAME canonical recipe",
              mrd_outcome.movements_created == 5 * 5)

        # ── 4. Wastage ────────────────────────────────────────────────────
        print("\n== WASTAGE: 2 cheese portions ==")
        db.add(build_movement(
            business_id=BIZ, ingredient_id=CHEESE,
            quantity_milli=-to_milli(2, "portion"),
            movement_type=StockMovementType.WASTAGE.value,
            unit_cost_micro_cents=ings["Cheese Portion"].cost_per_canonical_micro_cents,
            occurred_at=datetime.combine(SALE_DAY, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(hours=20)))
        await db.commit()
        check("cheese 65 -> 63 portions",
              await cheese_expected(db, BIZ, CHEESE) == to_milli(63, "portion"),
              str(await cheese_expected(db, BIZ, CHEESE)))

        # ── 5. THE DUPLICATE TEST ─────────────────────────────────────────
        print("\n== DUPLICATE UPLOAD: same Uber file again ==")
        before = await cheese_expected(db, BIZ, CHEESE)
        dup_summary, dup_outcome = await run_import(
            db, biz, ExternalPlatform.UBER_EATS.value, uber_csv(uber_rows(12345, 10)), owner)
        after = await cheese_expected(db, BIZ, CHEESE)
        check("all 10 recognised as already imported", len(dup_summary.duplicates) == 10,
              str(len(dup_summary.duplicates)))
        check("no new orders created", dup_outcome.orders_created == 0)
        check("ZERO additional deduction", before == after,
              f"{before} -> {after}")
        check("still 63 portions", after == to_milli(63, "portion"))

        # ── 6. Database-level guard, not just application logic ───────────
        print("\n== DUPLICATE GUARD AT THE DATABASE ==")
        async with Session() as db2:
            clash = Order(
                business_id=BIZ, order_number="X-DUP", status="COMPLETED",
                payment_status="PAID", source="UBER_EATS",
                external_order_id="UE-12345", total_cents=1,
                subtotal_cents=1, currency="ZAR",
            )
            db2.add(clash)
            try:
                await db2.commit()
                blocked = False
            except IntegrityError:
                await db2.rollback()
                blocked = True
        check("the database itself rejects a duplicate external order id", blocked)

        # ── 7. Concurrent double upload ───────────────────────────────────
        print("\n== CONCURRENT DOUBLE UPLOAD (two managers at once) ==")
        new_rows = uber_csv(uber_rows(77000, 3))

        async def concurrent_import():
            async with Session() as s:
                b = (await s.execute(select(Business).where(Business.id == BIZ))).scalar_one()
                try:
                    rows = await routes_import._read_rows(FakeUpload("f.csv", new_rows))
                    adapted = adapt_rows(rows, platform=ExternalPlatform.UBER_EATS.value)
                    summ = await resolve_orders(
                        s, business_id=BIZ, platform=ExternalPlatform.UBER_EATS.value,
                        orders=adapted.orders)
                    out = await import_resolved_orders(
                        s, business=b, platform=ExternalPlatform.UBER_EATS.value,
                        batch_id=uuid.uuid4(), summary=summ, user_id=owner.user_id)
                    await s.commit()
                    return out.orders_created
                except IntegrityError:
                    await s.rollback()
                    return 0

        before_conc = await cheese_expected(db, BIZ, CHEESE)
        results = await asyncio.gather(concurrent_import(), concurrent_import(),
                                       return_exceptions=True)
        created = [r for r in results if isinstance(r, int)]
        async with Session() as db3:
            after_conc = await cheese_expected(db3, BIZ, CHEESE)
        check("both attempts finished without corrupting anything", len(created) == 2, str(results))
        check("exactly 3 orders exist once", sum(created) == 3, f"created={created}")
        check("exactly 3 pizzas' worth deducted, not 6",
              after_conc - before_conc == -to_milli(3, "portion"),
              f"delta={after_conc - before_conc}")

        # ── 8. External money never reaches till cash-up ──────────────────
        print("\n== EXTERNAL PAYMENT ==")
        async with Session() as db4:
            ext_orders = (await db4.execute(
                select(Order).where(Order.business_id == BIZ,
                                    Order.source.in_(tuple(EXTERNAL_ORDER_SOURCES)))
            )).scalars().all()
            check("external orders exist", len(ext_orders) >= 15, str(len(ext_orders)))
            check("none is attached to a till session",
                  all(o.till_session_id is None for o in ext_orders))
            check("none records cash tendered or change",
                  all(o.cash_tendered_cents is None and o.change_due_cents is None
                      for o in ext_orders))
            check("all marked paid on the platform",
                  all(o.payment_method == "EXTERNAL_PLATFORM" and o.payment_status == "PAID"
                      for o in ext_orders))

            # ── 9. VAT stays inclusive ────────────────────────────────────
            print("\n== VAT ON EXTERNAL SALES ==")
            uber_order = next(o for o in ext_orders if o.source == "UBER_EATS")
            check("R149 Uber sale is recorded as R149, not inflated",
                  uber_order.total_cents == 14900, f"{uber_order.total_cents}")
            check("VAT derived from inside the price",
                  uber_order.tax_cents == calculate_vat_cents(14900, 1500, True),
                  f"{uber_order.tax_cents}")
            check("never becomes R171", uber_order.total_cents != 17135)

            # ── 10. Sale time, not upload time ────────────────────────────
            print("\n== SALE TIME vs IMPORT TIME ==")
            check("order keeps the platform's own placed-at",
                  uber_order.external_placed_at.date() == SALE_DAY,
                  str(uber_order.external_placed_at))
            check("import time recorded separately",
                  uber_order.external_imported_at is not None
                  and uber_order.external_imported_at > uber_order.external_placed_at)

            from shared.models.inventory import StockMovement
            mv = (await db4.execute(
                select(StockMovement).where(
                    StockMovement.business_id == BIZ,
                    StockMovement.source_id == uber_order.id).limit(1)
            )).scalar_one()
            check("stock movement dated when the sale happened",
                  mv.occurred_at.date() == SALE_DAY, str(mv.occurred_at))

        # ── 11. Stocktake -> variance -> rand value ───────────────────────
        print("\n== STOCKTAKE -> VARIANCE ==")
        async with Session() as db5:
            st = await routes_stocktake.open_stocktake(
                body=routes_stocktake.StocktakeOpenRequest(reference="Friday count"),
                user=owner, db=db5)
            counts = []
            for line in st.lines:
                expected_units = int(line.expected_quantity_milli / 1000)
                qty = expected_units - 4 if line.ingredient_name == "Cheese Portion" else expected_units
                counts.append(routes_stocktake.CountLineRequest(
                    ingredient_id=line.ingredient_id, quantity=str(qty)))
            await routes_stocktake.submit_counts(
                session_id=st.id, body=routes_stocktake.CountSubmitRequest(lines=counts),
                user=owner, db=db5)
            done = await routes_stocktake.complete_stocktake(session_id=st.id, user=owner, db=db5)
            cheese_line = next(l for l in done.lines if l.ingredient_name == "Cheese Portion")
            check("expected 60 portions (100 - 20 - 10 - 5 - 2 - 3 concurrent)",
                  cheese_line.expected_display == "60 portions", cheese_line.expected_display)
            check("variance -4 portions", cheese_line.variance_display == "-4 portions",
                  cheese_line.variance_display)
            check("potential loss R60 at R15/portion",
                  cheese_line.variance_value_cents == -6000,
                  str(cheese_line.variance_value_cents))
            report = await routes_stocktake.get_variance_report(
                session_id=st.id, flagged_only=False, user=owner, db=db5)
            check("never labelled theft",
                  not any(w in str(report).lower() for w in ("theft", "stolen", "steal")))
            await routes_stocktake.approve_stocktake(
                session_id=st.id, post_adjustments=True, user=owner, db=db5)

        # ── 12. Late import into a CLOSED stocktake period ────────────────
        print("\n== LATE IMPORT INTO A CLOSED PERIOD ==")
        async with Session() as db6:
            b = (await db6.execute(select(Business).where(Business.id == BIZ))).scalar_one()
            late = uber_csv(uber_rows(88000, 2, when=SALE_DAY_STR, time="17:00"))
            rows = await routes_import._read_rows(FakeUpload("late.csv", late))
            adapted = adapt_rows(rows, platform=ExternalPlatform.UBER_EATS.value)
            summ = await resolve_orders(
                db6, business_id=BIZ, platform=ExternalPlatform.UBER_EATS.value,
                orders=adapted.orders)
            check("late orders detected as inside a closed period",
                  len(summ.closed_period_orders) == 2, str(len(summ.closed_period_orders)))

            unack = await import_resolved_orders(
                db6, business=b, platform=ExternalPlatform.UBER_EATS.value,
                batch_id=uuid.uuid4(), summary=summ, user_id=owner.user_id,
                acknowledge_closed_period=False)
            await db6.commit()
            check("without acknowledgement nothing is imported", unack.orders_created == 0)

            before_late = await cheese_expected(db6, BIZ, CHEESE)
            ack = await import_resolved_orders(
                db6, business=b, platform=ExternalPlatform.UBER_EATS.value,
                batch_id=uuid.uuid4(), summary=summ, user_id=owner.user_id,
                acknowledge_closed_period=True)
            await db6.commit()
            after_late = await cheese_expected(db6, BIZ, CHEESE)
            check("with acknowledgement they import", ack.orders_created == 2)
            check("flagged as backdated", ack.backdated_into_closed_period == 2)
            check("ingredients were NOT lost", after_late - before_late == -to_milli(2, "portion"),
                  f"delta={after_late - before_late}")

            approved = (await db6.execute(
                select(routes_stocktake.StocktakeSession).where(
                    routes_stocktake.StocktakeSession.id == st.id))).scalar_one()
            check("the approved stocktake was NOT rewritten",
                  approved.status == "APPROVED"
                  and approved.total_variance_value_cents == done.total_variance_value_cents,
                  f"{approved.total_variance_value_cents} vs {done.total_variance_value_cents}")

            from shared.models.inventory import StockMovement
            late_order = (await db6.execute(
                select(Order).where(Order.business_id == BIZ,
                                    Order.external_order_id == "UE-88000"))).scalar_one()
            late_mv = (await db6.execute(
                select(StockMovement).where(
                    StockMovement.source_id == late_order.id).limit(1))).scalar_one()
            check("order keeps its true sale time",
                  late_order.external_placed_at.hour == 17, str(late_order.external_placed_at))
            check("but stock landed after the cut-off, in the open period",
                  late_mv.occurred_at > approved.cutoff_at,
                  f"{late_mv.occurred_at} > {approved.cutoff_at}")

        # ── 13. Tenant isolation ──────────────────────────────────────────
        print("\n== TENANT ISOLATION ==")
        async with Session() as db7:
            other_ext = (await db7.execute(
                select(func.count()).select_from(Order).where(
                    Order.business_id == OTHER,
                    Order.source.in_(tuple(EXTERNAL_ORDER_SOURCES))))).scalar()
            other_maps = (await db7.execute(
                select(func.count()).select_from(ExternalProductMapping).where(
                    ExternalProductMapping.business_id == OTHER))).scalar()
            check("no external orders leaked to the other tenant", int(other_ext or 0) == 0)
            check("no mappings leaked", int(other_maps or 0) == 0)

            # The SAME external id must be importable by a different tenant.
            b2 = (await db7.execute(select(Business).where(Business.id == OTHER))).scalar_one()
            summ2 = await resolve_orders(
                db7, business_id=OTHER, platform=ExternalPlatform.UBER_EATS.value,
                orders=adapt_rows(await routes_import._read_rows(
                    FakeUpload("f.csv", uber_csv(uber_rows(12345, 1)))),
                    platform=ExternalPlatform.UBER_EATS.value).orders)
            check("UE-12345 is NOT a duplicate for a different restaurant",
                  len(summ2.duplicates) == 0)
            check("but it is unmapped for them, so still blocked", len(summ2.blocked) == 1)

    await engine.dispose()
    print("\n" + "=" * 64)
    print(f"  {sum(OK)}/{len(OK)} checks passed")
    print("=" * 64)
    return 0 if all(OK) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
