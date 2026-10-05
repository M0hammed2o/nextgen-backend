"""
Release-gate proof (Part 2 of the release-gate brief): server-side proof, by
direct API/route-function call against a REAL Postgres, that each commercial
package NextGen actually sells enforces its entitlements -- never a check of
whether a nav item is hidden in a frontend, which proves nothing about the
API itself.

Package definitions used here are read from the code, not assumed -- see
docs/COMMERCIAL_FEATURE_MATRIX_2026-10.md section 2 ("Commercial package
mapping") plus the Business-column comments in shared/models/business.py
(inventory_enabled/digital_receipts_enabled/delivery_platforms_enabled/
advanced_analytics_enabled all say "same pattern as inventory_enabled" and
are business-plane READ-ONLY / platform-admin-writable only).

GAP FOUND WRITING THIS FILE, NOW CLOSED (migration 0051, KNOWN_BUGS.md
BUG-0107, Fixed)
----------------------------------------------------------------------
At the time this file was first written, Loyalty had NO Business-level
master-switch column, unlike inventory/WhatsApp/delivery/advanced-analytics
-- the only gate was `LoyaltyProgram.is_enabled`, a per-business config row
ANY OWNER could flip on themselves via `PUT /business/loyalty/program`, with
no package/entitlement check anywhere. `Business.loyalty_enabled` (migration
0051) now closes that gap the same way the other four are gated: platform-
admin-writable, business-plane READ-ONLY, enforced first in every route in
routes_loyalty.py and on the POS checkout redemption path. The tests below
now exercise the REAL platform-level gate, not merely "the owner never
turned it on."

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
"""
import uuid

import pytest
from sqlalchemy import delete
from unittest.mock import AsyncMock, patch

from backend.app.api.v1.routes_analytics import get_daily_breakdown, get_summary, get_top_items
from backend.app.api.v1.routes_delivery import list_connections
from backend.app.api.v1.routes_external_orders import channel_breakdown
from backend.app.api.v1.routes_inventory import (
    IngredientCreateRequest,
    OpeningStockRequest,
    SupplierCreateRequest,
    create_ingredient,
    create_supplier,
    list_ingredients,
    set_opening_stock,
)
from backend.app.api.v1.routes_loyalty import redeem_loyalty_reward, RedeemRequest
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSReceiptRequest,
    TillOpenRequest,
    create_pos_order,
    open_till,
    request_pos_receipt,
)
from backend.app.api.v1.routes_recipes import RecipeItemRequest, RecipeUpsertRequest, list_recipes, upsert_recipe
from backend.app.api.v1.routes_stocktake import (
    CountLineRequest,
    CountSubmitRequest,
    StocktakeOpenRequest,
    approve_stocktake,
    complete_stocktake,
    get_current_stocktake,
    list_stocktakes,
    open_stocktake,
    submit_counts,
)
from backend.app.api.v1.routes_wastage import WastageCreateRequest, approve_wastage, record_wastage
from backend.app.api.v1.routes_whatsapp_connect import get_connection_status
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.utils import generate_business_code


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


async def _make_business(db, **flags) -> Business:
    unique = uuid.uuid4().hex[:8]
    defaults = dict(
        name=f"Package Gate Biz {unique}", slug=f"package-gate-{unique}",
        business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
        # Explicit, never relying on column defaults, since is_whatsapp_enabled
        # defaults TRUE at the DB level (existing-tenant-safe default) -- a
        # POS-only package test must set it False itself to mean anything.
        is_whatsapp_enabled=False,
        inventory_enabled=False,
        digital_receipts_enabled=False,
        delivery_platforms_enabled=False,
        advanced_analytics_enabled=False,
        loyalty_enabled=False,
    )
    defaults.update(flags)
    business = Business(**defaults)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _cleanup(db, business_id):
    """
    Reuses the SAME `db` fixture session, never a second connection.

    IMPORTANT (found while writing this file): several routes above
    (`_get_open_till` in particular) take a `.with_for_update()` row lock
    that is only released on THIS session's next commit/rollback. A
    `pytest.raises(...)` block that catches an expected AppError raised
    AFTER such a lock was already taken (but before that call's own
    internal commit) leaves the transaction open and the lock held for the
    rest of the test. A cleanup routine that opened a SECOND, independent
    session to DELETE the same rows would then deadlock forever waiting on
    a lock this same test will only release once ITS OWN session rolls
    back or closes -- which never happens while cleanup is stuck waiting
    on it. Rolling back THIS session first, then deleting through it,
    avoids the deadlock entirely (same pattern already established by
    tests/integration/test_workflow_commercial.py's own `_cleanup(db, ...)`).
    """
    await db.rollback()
    from shared.models.customer import Customer
    from shared.models.inventory import GoodsReceipt, Ingredient, StockMovement, Supplier
    from shared.models.loyalty import LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
    from shared.models.menu import MenuCategory, MenuItem
    from shared.models.order import Order, OrderEvent, OrderItem
    from shared.models.recipe import Recipe, RecipeItem
    from shared.models.stocktake import StocktakeLine, StocktakeSession
    from shared.models.till import TillSession
    from shared.models.wastage import WastageEntry
    from sqlalchemy import select as _select

    await db.execute(delete(OrderItem).where(OrderItem.business_id == business_id))
    await db.execute(delete(OrderEvent).where(OrderEvent.business_id == business_id))
    await db.execute(delete(Order).where(Order.business_id == business_id))
    await db.execute(delete(TillSession).where(TillSession.business_id == business_id))
    await db.execute(delete(WastageEntry).where(WastageEntry.business_id == business_id))
    # StocktakeLine has no business_id column of its own -- delete via session join
    session_ids = (
        await db.execute(_select(StocktakeSession.id).where(StocktakeSession.business_id == business_id))
    ).scalars().all()
    if session_ids:
        await db.execute(delete(StocktakeLine).where(StocktakeLine.stocktake_session_id.in_(session_ids)))
    await db.execute(delete(StocktakeSession).where(StocktakeSession.business_id == business_id))
    await db.execute(delete(RecipeItem).where(
        RecipeItem.recipe_id.in_(_select(Recipe.id).where(Recipe.business_id == business_id))
    ))
    await db.execute(delete(Recipe).where(Recipe.business_id == business_id))
    await db.execute(delete(StockMovement).where(StockMovement.business_id == business_id))
    await db.execute(delete(GoodsReceipt).where(GoodsReceipt.business_id == business_id))
    await db.execute(delete(Ingredient).where(Ingredient.business_id == business_id))
    await db.execute(delete(Supplier).where(Supplier.business_id == business_id))
    await db.execute(delete(LoyaltyTransaction).where(LoyaltyTransaction.business_id == business_id))
    await db.execute(delete(LoyaltyReward).where(LoyaltyReward.business_id == business_id))
    await db.execute(delete(LoyaltyProgram).where(LoyaltyProgram.business_id == business_id))
    await db.execute(delete(Customer).where(Customer.business_id == business_id))
    await db.execute(delete(MenuItem).where(MenuItem.business_id == business_id))
    await db.execute(delete(MenuCategory).where(MenuCategory.business_id == business_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
    await db.execute(delete(Business).where(Business.id == business_id))
    await db.commit()


async def _menu_item(db, owner, price_cents=5000):
    category = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=db)
    return await create_item(
        body=ItemCreate(category_id=category.id, name="Gate Test Item", price_cents=price_cents),
        user=owner, db=db,
    )


# ── NextGen POS (R899): POS-only, everything else refused ───────────────────

async def test_nextgen_pos_package_refuses_everything_outside_pos(db):
    """
    Every module flag off. Assert: inventory / loyalty(-via-checkout) /
    WhatsApp / delivery / Advanced Analytics endpoints all correctly refuse,
    while core POS + till + printed receipts + Core Analytics work.
    """
    business = await _make_business(db)
    owner = _owner(business.id)
    try:
        # ── Core POS + till + printed receipt: MUST WORK ─────────────────
        item = await _menu_item(db, owner)
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
        assert till.status == "OPEN"

        order = await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=5000,
                idempotency_key=f"pos-pkg-{uuid.uuid4().hex[:8]}",
            ),
            user=owner, db=db,
        )
        assert order.total_cents == 5000
        assert order.payment_status == "PAID"

        receipt = await request_pos_receipt(
            order_id=order.id, body=POSReceiptRequest(channels=["PRINT"]), user=owner, db=db,
        )
        assert receipt.print_requested is True

        # ── Core Analytics: MUST WORK (never gated) ───────────────────────
        summary = await get_summary(period="30d", user=owner, db=db)
        assert summary.total_orders >= 1
        top_items = await get_top_items(days=30, limit=10, user=owner, db=db)
        assert isinstance(top_items, list)
        daily = await get_daily_breakdown(days=30, user=owner, db=db)
        assert isinstance(daily, list)

        # ── Inventory: MUST REFUSE ────────────────────────────────────────
        with pytest.raises(AppError) as exc:
            await list_ingredients(user=owner, db=db)
        assert exc.value.code == "INVENTORY_NOT_ENABLED"
        assert exc.value.status_code == 409

        # ── Stocktake reads: MUST REFUSE (KNOWN_BUGS.md BUG-0115) ──────────
        # Found by direct API probe 2026-09-26: every other inventory-module
        # endpoint refused a non-entitled tenant except these two reads,
        # which returned 200 ([] / null) instead of 409. Stocktake is a
        # NextGen Operations (R1,499) feature — a POS-only (R899) tenant
        # must be refused here exactly like every write below already was.
        with pytest.raises(AppError) as exc:
            await list_stocktakes(user=owner, db=db)
        assert exc.value.code == "INVENTORY_NOT_ENABLED"
        assert exc.value.status_code == 409

        with pytest.raises(AppError) as exc:
            await get_current_stocktake(user=owner, db=db)
        assert exc.value.code == "INVENTORY_NOT_ENABLED"
        assert exc.value.status_code == 409

        # ── Stocktake writes: MUST REFUSE (already correct — regression guard) ─
        with pytest.raises(AppError) as exc:
            await open_stocktake(body=StocktakeOpenRequest(), user=owner, db=db)
        assert exc.value.code == "INVENTORY_NOT_ENABLED"
        assert exc.value.status_code == 409

        # ── Loyalty (checkout redemption path): MUST REFUSE ───────────────
        # Business.loyalty_enabled defaults False (migration 0051,
        # KNOWN_BUGS.md BUG-0107) -- checked FIRST in create_pos_order,
        # before the till/business/payment-method logic or the per-program
        # LOYALTY_DISABLED check ever run, so this is a genuine platform-
        # level refusal, not merely "the owner never turned it on."
        with pytest.raises(AppError) as exc:
            await create_pos_order(
                body=POSOrderCreateRequest(
                    items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                    payment_method="CASH", cash_tendered_cents=5000,
                    customer_phone="0821230000",
                    loyalty_reward_id=uuid.uuid4(),
                    idempotency_key=f"pos-pkg-loyalty-{uuid.uuid4().hex[:8]}",
                ),
                user=owner, db=db,
            )
        assert exc.value.code == "LOYALTY_NOT_ENABLED"
        assert exc.value.status_code == 409
        # The refused create_pos_order call above already took (and, since it
        # raised before its own internal commit, never released) a
        # `.with_for_update()` lock on the till row via `_get_open_till` --
        # roll back explicitly so it doesn't outlive this call and block
        # this test's own cleanup later (see _cleanup's docstring).
        await db.rollback()

        # ── WhatsApp: MUST REFUSE ──────────────────────────────────────────
        with pytest.raises(AppError) as exc:
            await get_connection_status(user=owner, db=db)
        assert exc.value.code == "WHATSAPP_NOT_ENABLED"
        assert exc.value.status_code == 409

        # ── Delivery marketplace: MUST REFUSE ──────────────────────────────
        with pytest.raises(AppError) as exc:
            await list_connections(user=owner, db=db)
        assert exc.value.code == "DELIVERY_NOT_ENABLED"
        assert exc.value.status_code == 409

        # ── Advanced Analytics: MUST REFUSE ────────────────────────────────
        from backend.app.api.v1.routes_analytics import get_average_order_value, get_payment_method_breakdown
        with pytest.raises(AppError) as exc:
            await get_average_order_value(period="30d", user=owner, db=db)
        assert exc.value.code == "ADVANCED_ANALYTICS_NOT_ENABLED"
        assert exc.value.status_code == 409

        with pytest.raises(AppError) as exc:
            await get_payment_method_breakdown(days=30, user=owner, db=db)
        assert exc.value.code == "ADVANCED_ANALYTICS_NOT_ENABLED"

        with pytest.raises(AppError) as exc:
            await channel_breakdown(days=30, user=owner, db=db)
        assert exc.value.code == "ADVANCED_ANALYTICS_NOT_ENABLED"
    finally:
        await _cleanup(db, business.id)


# ── NextGen Operations (R1,499): POS + inventory, still no Complete-tier ────

async def test_nextgen_operations_package_unlocks_inventory_but_not_complete_tier(db):
    business = await _make_business(db, inventory_enabled=True)
    owner = _owner(business.id)
    try:
        # ── Inventory / suppliers / opening stock: MUST WORK ──────────────
        ingredient = await create_ingredient(
            body=IngredientCreateRequest(
                name="Gate Test Flour", canonical_unit="GRAM",
                purchase_pack_quantity="1", purchase_pack_unit="KG", cost_per_pack_cents=5000,
            ),
            user=owner, db=db,
        )
        assert ingredient.id is not None

        supplier = await create_supplier(
            body=SupplierCreateRequest(name="Gate Test Supplier"), user=owner, db=db,
        )
        assert supplier.id is not None

        opening = await set_opening_stock(
            body=OpeningStockRequest(ingredient_id=ingredient.id, quantity="10", unit="KG"),
            user=owner, db=db,
        )
        assert opening.quantity_milli == 10_000_000  # 10 kg in milli-grams

        # ── Recipes: MUST WORK ─────────────────────────────────────────────
        item = await _menu_item(db, owner, price_cents=6000)
        recipe = await upsert_recipe(
            body=RecipeUpsertRequest(
                menu_item_id=item.id, yield_qty=1,
                items=[RecipeItemRequest(ingredient_id=ingredient.id, quantity="100", unit="GRAM")],
            ),
            user=owner, db=db,
        )
        assert recipe.items[0].ingredient_id == ingredient.id
        recipes = await list_recipes(user=owner, db=db)
        assert any(r.id == recipe.id for r in recipes)

        # ── Stocktake full lifecycle: MUST WORK ────────────────────────────
        stocktake = await open_stocktake(body=StocktakeOpenRequest(reference="Gate Test Count"), user=owner, db=db)
        assert stocktake.status == "DRAFT"

        # ── Stocktake READS: MUST WORK for an entitled tenant (BUG-0115 ────
        # non-regression — the fix must not break the paying Operations
        # customer the gate is meant to let through) ───────────────────────
        current = await get_current_stocktake(user=owner, db=db)
        assert current is not None and current.id == stocktake.id

        stocktake = await submit_counts(
            session_id=stocktake.id,
            body=CountSubmitRequest(lines=[CountLineRequest(ingredient_id=ingredient.id, quantity="10", unit="KG")]),
            user=owner, db=db,
        )
        stocktake = await complete_stocktake(session_id=stocktake.id, user=owner, db=db)
        assert stocktake.status == "COMPLETED"
        stocktake = await approve_stocktake(session_id=stocktake.id, user=owner, db=db)
        assert stocktake.status == "APPROVED"

        history = await list_stocktakes(limit=50, user=owner, db=db)
        assert any(row["id"] == str(stocktake.id) for row in history), (
            "list_stocktakes must still return an entitled tenant's own "
            "history after the BUG-0115 fix"
        )

        # ── Wastage + approval queue: MUST WORK ────────────────────────────
        biz_row = await db.get(Business, business.id)
        biz_row.wastage_approval_threshold_cents = 100  # force the approval path
        await db.commit()
        staff = _staff(business.id)
        wastage = await record_wastage(
            body=WastageCreateRequest(ingredient_id=ingredient.id, quantity="500", unit="G", reason="DROPPED"),
            user=staff, db=db,
        )
        assert wastage.approved_by_user_id is None  # STAFF entry above threshold: pending
        approved = await approve_wastage(entry_id=wastage.id, user=owner, db=db)
        assert approved.approved_by_user_id == owner.user_id

        # ── Still NOT Complete-tier: loyalty / WhatsApp / delivery /
        #    Advanced Analytics must all still refuse ───────────────────────
        with pytest.raises(AppError) as exc:
            await get_connection_status(user=owner, db=db)
        assert exc.value.code == "WHATSAPP_NOT_ENABLED"

        with pytest.raises(AppError) as exc:
            await list_connections(user=owner, db=db)
        assert exc.value.code == "DELIVERY_NOT_ENABLED"

        from backend.app.api.v1.routes_analytics import get_average_order_value
        with pytest.raises(AppError) as exc:
            await get_average_order_value(period="30d", user=owner, db=db)
        assert exc.value.code == "ADVANCED_ANALYTICS_NOT_ENABLED"
    finally:
        await _cleanup(db, business.id)


# ── NextGen Complete (from R2,499): everything unlocked ──────────────────────

async def test_nextgen_complete_package_unlocks_whatsapp_loyalty_delivery_and_advanced_analytics(db):
    business = await _make_business(
        db, inventory_enabled=True, is_whatsapp_enabled=True,
        digital_receipts_enabled=True, delivery_platforms_enabled=True,
        advanced_analytics_enabled=True,
        # migration 0051, KNOWN_BUGS.md BUG-0107 — Loyalty is exercised
        # below via update_loyalty_program/create_reward/checkout
        # redemption, all of which now require platform entitlement first.
        loyalty_enabled=True,
    )
    owner = _owner(business.id)
    try:
        # ── Stocktake reads: MUST WORK (BUG-0115 non-regression on the ─────
        # top commercial tier too — inventory_enabled=True here as well) ───
        assert await list_stocktakes(limit=50, user=owner, db=db) == []
        assert await get_current_stocktake(user=owner, db=db) is None

        # ── WhatsApp connect: MUST WORK (no longer refused) ────────────────
        status = await get_connection_status(user=owner, db=db)
        assert status.whatsapp_module_enabled is True

        # ── Delivery marketplace: MUST WORK ─────────────────────────────────
        connections = await list_connections(user=owner, db=db)
        assert isinstance(connections, list)

        # ── Advanced Analytics: MUST WORK ────────────────────────────────────
        from backend.app.api.v1.routes_analytics import get_average_order_value, get_payment_method_breakdown
        aov = await get_average_order_value(period="30d", user=owner, db=db)
        assert aov is not None
        payment_methods = await get_payment_method_breakdown(days=30, user=owner, db=db)
        assert payment_methods is not None
        channels = await channel_breakdown(days=30, user=owner, db=db)
        assert channels is not None

        # ── Loyalty redemption at checkout: MUST WORK ────────────────────────
        from backend.app.api.v1.routes_loyalty import LoyaltyProgramUpdate, LoyaltyRewardCreate, create_reward, update_loyalty_program
        from shared.models.customer import Customer

        await update_loyalty_program(
            body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=0, points_per_order=0, min_redemption_points=0),
            user=owner, db=db,
        )
        reward = await create_reward(
            body=LoyaltyRewardCreate(name="Complete Tier Reward", points_cost=100, value_cents=1000),
            user=owner, db=db,
        )
        customer = Customer(
            business_id=business.id, wa_id=None, phone_number="+27821239999",
            loyalty_points_balance=500, loyalty_tier="BRONZE",
        )
        db.add(customer)
        await db.commit()

        item = await _menu_item(db, owner, price_cents=8000)
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
        order = await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CARD",
                customer_phone="0821239999",
                loyalty_reward_id=reward.id,
                idempotency_key=f"complete-pkg-{uuid.uuid4().hex[:8]}",
            ),
            user=owner, db=db,
        )
        assert order.loyalty_discount_cents == 1000
        assert order.total_cents == 7000  # 8000 - 1000 discount
    finally:
        await _cleanup(db, business.id)


# ── Paperless Receipts (R199): standalone, does NOT require Operations/Complete ─

async def test_paperless_receipts_standalone_on_a_pos_only_business(db):
    """
    digital_receipts_enabled must work as a standalone add-on -- a POS-only
    business (inventory/WhatsApp/delivery/advanced-analytics ALL still off)
    must still be able to attempt a WhatsApp digital-receipt send once this
    one flag is on. Proven by asserting the send is genuinely ATTEMPTED
    (the Meta-boundary mock is called and the receipt leaves NOT_REQUESTED)
    -- never by faking a third-party success; the mocked boundary here
    returns a realistic failure (no live Meta credentials exist in this
    environment), same pattern as tests/integration/test_digital_receipts_realdb.py.
    """
    # whatsapp_phone_number_id is plain Model-1 send infrastructure, not a
    # module entitlement -- setting it here (while is_whatsapp_enabled stays
    # False) is what makes this an honest proof of the commercial claim:
    # digital receipts send via WhatsApp on their own phone_number_id even
    # when the WhatsApp ORDERING BOT module is off.
    business = await _make_business(
        db, digital_receipts_enabled=True, whatsapp_phone_number_id=f"paperless-{uuid.uuid4().hex[:10]}",
    )
    owner = _owner(business.id)
    try:
        assert business.inventory_enabled is False
        assert business.is_whatsapp_enabled is False
        assert business.delivery_platforms_enabled is False
        assert business.advanced_analytics_enabled is False

        item = await _menu_item(db, owner, price_cents=4000)
        await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
        order = await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=4000,
                customer_phone="0821237777",
                idempotency_key=f"paperless-{uuid.uuid4().hex[:8]}",
            ),
            user=owner, db=db,
        )

        from backend.app.bot.meta_errors import MetaErrorInfo

        no_creds_failure = AsyncMock(
            return_value=(
                None,
                MetaErrorInfo(
                    status_code=401, code=None, subcode=None,
                    message="no live Meta credentials in this test environment",
                    details=None, retryable=False,
                    last_error="TEST: no live Meta credentials in this test environment",
                ),
            )
        )
        with patch("backend.app.bot.whatsapp_sender.send_template_message", no_creds_failure):
            receipt = await request_pos_receipt(
                order_id=order.id, body=POSReceiptRequest(channels=["WHATSAPP"]), user=owner, db=db,
            )

        assert no_creds_failure.called, (
            "digital_receipts_enabled=True must let the WhatsApp channel actually "
            "ATTEMPT a send on a POS-only business -- proving it does not silently "
            "require Operations or Complete"
        )
        assert receipt.whatsapp_status == "FAILED"  # real attempt, real (mocked) failure -- never a faked SENT
        assert receipt.whatsapp_status != "NOT_REQUESTED"
    finally:
        await _cleanup(db, business.id)


async def test_digital_receipts_not_attempted_when_flag_is_off(db):
    """The negative control for the standalone-independence test above."""
    business = await _make_business(db)  # every flag off, including digital_receipts_enabled
    owner = _owner(business.id)
    try:
        item = await _menu_item(db, owner, price_cents=4000)
        await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner, db=db)
        order = await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                payment_method="CASH", cash_tendered_cents=4000,
                customer_phone="0821236666",
                idempotency_key=f"paperless-off-{uuid.uuid4().hex[:8]}",
            ),
            user=owner, db=db,
        )

        never_call = AsyncMock(side_effect=AssertionError("must never attempt a send when the flag is off"))
        with patch("backend.app.bot.whatsapp_sender.send_template_message", never_call):
            receipt = await request_pos_receipt(
                order_id=order.id, body=POSReceiptRequest(channels=["WHATSAPP"]), user=owner, db=db,
            )

        assert not never_call.called
        assert receipt.whatsapp_status == "NOT_REQUESTED"
    finally:
        await _cleanup(db, business.id)
