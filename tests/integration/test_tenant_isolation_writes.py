"""
Wave 0A tenant-isolation WRITE-defect regression tests — real Postgres.

Proves Restaurant A can never reference, modify, attach, or detach
Restaurant B's resources by id, for every route touched in this hardening
pass: menu add-ons (attach AND detach), menu categories (item create +
update), suppliers (goods receipt), reference assets (business settings,
specials image), wastage references (photo asset, related order), push
subscriptions (subscribe re-point, unsubscribe), and the CSV export
OrderItem sub-query (BUG-0005).

A mocked session cannot honestly prove a WHERE-clause tenant filter — it
just returns whatever canned value a test wires up — so these run against
a REAL Postgres database. They read the target from the TEST_DB_URL env
var, defaulting to the Wave 0A throwaway local container. If that database
is unreachable, every test in this module SKIPS cleanly (not fails), so
the default mocked suite (`pytest tests --ignore=tests/replay
--ignore=tests/integration`) stays green on machines without it.

This module intentionally builds its own engine/session (rather than
importing `backend.app.db.session.engine`), because whichever DATABASE_URL
gets resolved first at collection time — real env var, or the generic
placeholder `tests/conftest.py` falls back to — controls what that shared,
already-imported engine is bound to for the rest of the process. Reading
TEST_DB_URL independently guarantees this module talks to the intended
throwaway database regardless of collection order.

Run directly with:
    python -m pytest tests/integration/test_tenant_isolation_writes.py -q
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_TEST_DB_URL = "postgresql+asyncpg://nextgen:wave0a_local_only@localhost:55444/nextgen_test"
TEST_DB_URL = os.environ.get("TEST_DB_URL", DEFAULT_TEST_DB_URL)

_engine = create_async_engine(TEST_DB_URL, pool_pre_ping=True, connect_args={"statement_cache_size": 0})
_session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)

from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_export import export_orders_csv
from backend.app.api.v1.routes_inventory import (
    GoodsReceiptCreateRequest,
    GoodsReceiptLineRequest,
    IngredientCreateRequest,
    SupplierCreateRequest,
    create_goods_receipt,
    create_ingredient,
    create_supplier,
)
from backend.app.api.v1.routes_menu import (
    AddOnCreate,
    CategoryCreate,
    ItemCreate,
    ItemUpdate,
    attach_add_on,
    create_add_on,
    create_category,
    create_item,
    detach_add_on,
    update_item,
)
from backend.app.api.v1.routes_push import SubscribeRequest, UnsubscribeRequest, subscribe, unsubscribe
from backend.app.api.v1.routes_specials import SpecialCreate, create_special
from backend.app.api.v1.routes_wastage import WastageCreateRequest, record_wastage
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.asset import Asset
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.inventory import GoodsReceipt
from shared.models.menu import MenuItem, menu_item_add_ons
from shared.models.order import Order, OrderItem
from shared.models.push_subscription import PushSubscription
from shared.models.specials import Special
from shared.models.wastage import WastageEntry
from shared.utils import generate_business_code


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    try:
        async with _engine.connect():
            pass
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Wave 0A throwaway DB unreachable at {TEST_DB_URL}: {exc}")

    async with _session_factory() as session:
        yield session
        await session.rollback()
        await session.close()

    # Same reasoning as tests/integration/conftest.py's `db` fixture: dispose
    # the pool after every test so the next test (its own event loop, under
    # pytest-asyncio's function-scoped loop) opens fresh connections.
    await _engine.dispose()


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Wave0A Isolation Test {unique}",
        slug=f"wave0a-isolation-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        **overrides,
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _cleanup_business(db: AsyncSession, business_id: uuid.UUID) -> None:
    # AuditEvent.business_id is ON DELETE SET NULL, not CASCADE — delete it
    # explicitly first (see tests/integration/conftest.py's test_business
    # fixture for the same reasoning).
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
    await db.execute(delete(Business).where(Business.id == business_id))
    await db.commit()


@pytest.fixture
async def two_businesses(db: AsyncSession):
    """Two independent, real, throwaway tenants — cleaned up unconditionally."""
    biz_a = await _make_business(db, inventory_enabled=True)
    biz_b = await _make_business(db, inventory_enabled=True)
    try:
        yield biz_a, biz_b
    finally:
        try:
            await _cleanup_business(db, biz_a.id)
            await _cleanup_business(db, biz_b.id)
        except Exception:
            # A test that raised mid-way (e.g. an unexpected IntegrityError)
            # can leave the session's transaction aborted — every further
            # statement on it fails until it's rolled back, which would
            # otherwise leak these rows silently instead of cleaning them
            # up. Only roll back on this (retry) path — unconditionally
            # rolling back a session that has nothing to roll back confuses
            # asyncpg's connection-pool pre-ping on the next checkout.
            await db.rollback()
            await _cleanup_business(db, biz_a.id)
            await _cleanup_business(db, biz_b.id)


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


async def _make_persisted_staff(db: AsyncSession, business_id: uuid.UUID) -> AuthUser:
    """
    Like _staff(), but backs the AuthUser with a real, committed BusinessUser
    row. Needed wherever the row under test has a real foreign key to
    business_users.id (e.g. PushSubscription.user_id) — most routes only
    store the user id as a plain UUID with no FK, where a random id is fine.
    """
    from shared.models.user import BusinessUser

    user_row = BusinessUser(
        business_id=business_id, role="STAFF", staff_name=f"Test Staff {uuid.uuid4().hex[:8]}",
    )
    db.add(user_row)
    await db.commit()
    await db.refresh(user_row)
    return AuthUser(user_id=user_row.id, business_id=business_id, role="STAFF", token_type="access")


# ── Menu add-ons: attach / detach ───────────────────────────────────────────

async def test_attach_add_on_rejects_cross_tenant_add_on(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, owner_b = _owner(biz_a.id), _owner(biz_b.id)

    item_a = await create_item(body=ItemCreate(name="Pizza", price_cents=10000), user=owner_a, db=db)
    add_on_b = await create_add_on(body=AddOnCreate(name="Extra Cheese", price_cents=1000), user=owner_b, db=db)

    with pytest.raises(NotFoundError):
        await attach_add_on(item_id=item_a.id, add_on_id=add_on_b.id, user=owner_a, db=db)

    joined = (
        await db.execute(select(menu_item_add_ons).where(menu_item_add_ons.c.add_on_id == add_on_b.id))
    ).all()
    assert joined == []


async def test_detach_add_on_rejects_cross_tenant_caller_and_leaves_join_intact(db, two_businesses):
    """
    The core BUG under test: detach_add_on previously deleted the join row
    by (item_id, add_on_id) alone, with no business check at all — any
    owner/manager from ANY business could detach ANY other business's
    add-on link just by knowing/guessing its two UUIDs.
    """
    biz_a, biz_b = two_businesses
    owner_a, owner_b = _owner(biz_a.id), _owner(biz_b.id)

    item_a = await create_item(body=ItemCreate(name="Pizza", price_cents=10000), user=owner_a, db=db)
    add_on_a = await create_add_on(body=AddOnCreate(name="Extra Cheese", price_cents=1000), user=owner_a, db=db)
    await attach_add_on(item_id=item_a.id, add_on_id=add_on_a.id, user=owner_a, db=db)

    with pytest.raises(NotFoundError):
        await detach_add_on(item_id=item_a.id, add_on_id=add_on_a.id, user=owner_b, db=db)

    still_joined = (
        await db.execute(
            select(menu_item_add_ons).where(
                menu_item_add_ons.c.menu_item_id == item_a.id,
                menu_item_add_ons.c.add_on_id == add_on_a.id,
            )
        )
    ).all()
    assert len(still_joined) == 1


# ── Menu categories: item create + update ───────────────────────────────────

async def test_create_item_rejects_cross_tenant_category(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, owner_b = _owner(biz_a.id), _owner(biz_b.id)

    category_b = await create_category(body=CategoryCreate(name="Mains"), user=owner_b, db=db)

    with pytest.raises(NotFoundError):
        await create_item(
            body=ItemCreate(name="Steak", price_cents=5000, category_id=category_b.id),
            user=owner_a, db=db,
        )

    leaked = (
        await db.execute(select(MenuItem).where(MenuItem.category_id == category_b.id))
    ).scalars().all()
    assert leaked == []


async def test_update_item_rejects_cross_tenant_category(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, owner_b = _owner(biz_a.id), _owner(biz_b.id)

    item_a = await create_item(body=ItemCreate(name="Pizza", price_cents=8000), user=owner_a, db=db)
    category_b = await create_category(body=CategoryCreate(name="Desserts"), user=owner_b, db=db)

    with pytest.raises(NotFoundError):
        await update_item(item_id=item_a.id, body=ItemUpdate(category_id=category_b.id), user=owner_a, db=db)

    row = (await db.execute(select(MenuItem).where(MenuItem.id == item_a.id))).scalar_one()
    assert row.category_id is None


# ── Inventory: goods receipt supplier_id ────────────────────────────────────

async def test_goods_receipt_rejects_cross_tenant_supplier(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, staff_a, owner_b = _owner(biz_a.id), _staff(biz_a.id), _owner(biz_b.id)

    ingredient_a = await create_ingredient(
        body=IngredientCreateRequest(name="Flour", canonical_unit="UNIT"), user=owner_a, db=db
    )
    supplier_b = await create_supplier(body=SupplierCreateRequest(name="ACME Supplies"), user=owner_b, db=db)

    with pytest.raises(NotFoundError):
        await create_goods_receipt(
            body=GoodsReceiptCreateRequest(
                lines=[GoodsReceiptLineRequest(ingredient_id=ingredient_a.id, quantity="1", unit="UNIT")],
                supplier_id=supplier_b.id,
            ),
            user=staff_a, db=db,
        )

    receipts = (
        await db.execute(select(GoodsReceipt).where(GoodsReceipt.supplier_id == supplier_b.id))
    ).scalars().all()
    assert receipts == []


# ── Business settings: reference assets ─────────────────────────────────────

async def test_update_business_settings_rejects_cross_tenant_reference_asset(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a = _owner(biz_a.id)

    asset_b = Asset(
        business_id=biz_b.id, kind="RECEIPT_REFERENCE",
        storage_path="b/reference/receipt.pdf", content_type="application/pdf",
    )
    db.add(asset_b)
    await db.commit()
    await db.refresh(asset_b)

    with pytest.raises(NotFoundError):
        await update_business_settings(
            body=BusinessSettingsUpdate(receipt_reference_asset_id=asset_b.id), user=owner_a, db=db,
        )

    row = (await db.execute(select(Business).where(Business.id == biz_a.id))).scalar_one()
    assert row.receipt_reference_asset_id is None


# ── Specials: image_asset_id ─────────────────────────────────────────────────

async def test_create_special_rejects_cross_tenant_image_asset(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a = _owner(biz_a.id)

    asset_b = Asset(
        business_id=biz_b.id, kind="SPECIAL_IMAGE",
        storage_path="b/special/promo.jpg", content_type="image/jpeg",
    )
    db.add(asset_b)
    await db.commit()
    await db.refresh(asset_b)

    with pytest.raises(NotFoundError):
        await create_special(
            body=SpecialCreate(title="Taco Tuesday", image_asset_id=asset_b.id), user=owner_a, db=db,
        )

    leaked = (await db.execute(select(Special).where(Special.image_asset_id == asset_b.id))).scalars().all()
    assert leaked == []


# ── Wastage: photo_asset_id / related_order_id ──────────────────────────────

async def test_record_wastage_rejects_cross_tenant_photo_asset(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, staff_a = _owner(biz_a.id), _staff(biz_a.id)

    ingredient_a = await create_ingredient(
        body=IngredientCreateRequest(name="Cheese", canonical_unit="UNIT"), user=owner_a, db=db
    )
    asset_b = Asset(
        business_id=biz_b.id, kind="WASTAGE_PHOTO",
        storage_path="b/wastage/dropped.jpg", content_type="image/jpeg",
    )
    db.add(asset_b)
    await db.commit()
    await db.refresh(asset_b)

    with pytest.raises(NotFoundError):
        await record_wastage(
            body=WastageCreateRequest(
                ingredient_id=ingredient_a.id, quantity="1", unit="UNIT",
                reason="DROPPED", photo_asset_id=asset_b.id,
            ),
            user=staff_a, db=db,
        )

    entries = (
        await db.execute(select(WastageEntry).where(WastageEntry.photo_asset_id == asset_b.id))
    ).scalars().all()
    assert entries == []


async def test_record_wastage_rejects_cross_tenant_related_order(db, two_businesses):
    biz_a, biz_b = two_businesses
    owner_a, staff_a = _owner(biz_a.id), _staff(biz_a.id)

    ingredient_a = await create_ingredient(
        body=IngredientCreateRequest(name="Dough", canonical_unit="UNIT"), user=owner_a, db=db
    )
    order_b = Order(business_id=biz_b.id, order_number="B-0001")
    db.add(order_b)
    await db.commit()
    await db.refresh(order_b)

    with pytest.raises(NotFoundError):
        await record_wastage(
            body=WastageCreateRequest(
                ingredient_id=ingredient_a.id, quantity="1", unit="UNIT",
                reason="DROPPED", related_order_id=order_b.id,
            ),
            user=staff_a, db=db,
        )

    entries = (
        await db.execute(select(WastageEntry).where(WastageEntry.related_order_id == order_b.id))
    ).scalars().all()
    assert entries == []


# ── Push subscriptions ───────────────────────────────────────────────────────

async def test_push_subscribe_rejects_cross_tenant_repoint_with_mismatched_keys(db, two_businesses):
    """
    The threat this closes: a caller who merely knows/guesses a victim's
    endpoint URL must not be able to steal that subscription by re-pointing
    it to their own (different) business.
    """
    biz_a, biz_b = two_businesses
    victim_staff = await _make_persisted_staff(db, biz_b.id)
    attacker_staff = await _make_persisted_staff(db, biz_a.id)

    endpoint = f"https://push.example/{uuid.uuid4().hex}"
    victim_sub = PushSubscription(
        business_id=biz_b.id, user_id=victim_staff.user_id,
        endpoint=endpoint, p256dh="victim-p256dh", auth="victim-auth",
    )
    db.add(victim_sub)
    await db.commit()

    result = await subscribe(
        body=SubscribeRequest(endpoint=endpoint, p256dh="attacker-p256dh", auth="attacker-auth"),
        user=attacker_staff, db=db,
    )
    assert result == {"subscribed": True}  # non-revealing — identical to a real success

    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    ).scalar_one()
    assert row.business_id == biz_b.id  # victim row untouched
    assert row.user_id == victim_staff.user_id
    assert row.p256dh == "victim-p256dh"
    assert row.auth == "victim-auth"


async def test_push_subscribe_allows_cross_tenant_repoint_when_keys_match(db, two_businesses):
    """
    Legitimate case: the same physical browser subscription (proven by
    presenting the exact stored p256dh/auth keys) moves to a new business
    when the device is reused elsewhere.
    """
    biz_a, biz_b = two_businesses
    old_staff = await _make_persisted_staff(db, biz_a.id)
    new_staff = await _make_persisted_staff(db, biz_b.id)

    endpoint = f"https://push.example/{uuid.uuid4().hex}"
    sub = PushSubscription(
        business_id=biz_a.id, user_id=old_staff.user_id,
        endpoint=endpoint, p256dh="shared-p256dh", auth="shared-auth",
    )
    db.add(sub)
    await db.commit()

    result = await subscribe(
        body=SubscribeRequest(endpoint=endpoint, p256dh="shared-p256dh", auth="shared-auth"),
        user=new_staff, db=db,
    )
    assert result == {"subscribed": True}

    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    ).scalar_one()
    assert row.business_id == biz_b.id
    assert row.user_id == new_staff.user_id


async def test_push_subscribe_allows_same_business_repoint(db, two_businesses):
    """Ordinary staff handover on a shared till device stays allowed."""
    biz_a, _biz_b = two_businesses
    staff1 = await _make_persisted_staff(db, biz_a.id)
    staff2 = await _make_persisted_staff(db, biz_a.id)

    endpoint = f"https://push.example/{uuid.uuid4().hex}"
    sub = PushSubscription(
        business_id=biz_a.id, user_id=staff1.user_id, endpoint=endpoint, p256dh="k1", auth="a1"
    )
    db.add(sub)
    await db.commit()

    await subscribe(body=SubscribeRequest(endpoint=endpoint, p256dh="k2", auth="a2"), user=staff2, db=db)

    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    ).scalar_one()
    assert row.business_id == biz_a.id
    assert row.user_id == staff2.user_id
    assert row.p256dh == "k2"


async def test_unsubscribe_only_deletes_caller_business_row(db, two_businesses):
    """Regression guard: unsubscribe already filtered by business_id — must keep doing so."""
    biz_a, biz_b = two_businesses
    victim_staff = await _make_persisted_staff(db, biz_b.id)
    attacker_staff = _staff(biz_a.id)

    endpoint = f"https://push.example/{uuid.uuid4().hex}"
    sub = PushSubscription(
        business_id=biz_b.id, user_id=victim_staff.user_id, endpoint=endpoint, p256dh="k", auth="a"
    )
    db.add(sub)
    await db.commit()

    await unsubscribe(body=UnsubscribeRequest(endpoint=endpoint), user=attacker_staff, db=db)

    row = (
        await db.execute(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    ).scalar_one_or_none()
    assert row is not None  # victim's row survives a cross-tenant unsubscribe attempt
    assert row.business_id == biz_b.id


# ── Export: OrderItem sub-query business_id filter (BUG-0005) ──────────────

async def test_export_csv_orderitem_subquery_filters_by_business_id(db, two_businesses):
    """
    Defense-in-depth regression: even if an OrderItem row somehow carried a
    business_id that didn't match its parent Order's business_id (a data
    integrity anomaly that should never happen through normal writes, but
    is exactly what BUG-0005's missing filter would have silently
    included), the export must never surface it.
    """
    biz_a, biz_b = two_businesses
    owner_a = _owner(biz_a.id)

    order_a = Order(business_id=biz_a.id, order_number="A-0001")
    db.add(order_a)
    await db.commit()
    await db.refresh(order_a)

    good_item = OrderItem(
        order_id=order_a.id, business_id=biz_a.id, name_snapshot="Real Pizza",
        unit_price_cents=1000, quantity=1, line_total_cents=1000,
    )
    anomalous_item = OrderItem(
        order_id=order_a.id, business_id=biz_b.id, name_snapshot="Leaked Item",
        unit_price_cents=1000, quantity=1, line_total_cents=1000,
    )
    db.add_all([good_item, anomalous_item])
    await db.commit()

    response = await export_orders_csv(days=365, status=None, user=owner_a, db=db)
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(
        chunk.encode() if isinstance(chunk, str) else chunk for chunk in chunks
    ).decode()

    assert "Real Pizza" in body
    assert "Leaked Item" not in body
