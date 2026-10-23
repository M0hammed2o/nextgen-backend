"""
Real-DB coverage for the new multi-page menu-images feature
(routes_menu_images.py, BusinessMenuImage, migration 0058).

Proves, against a real Postgres (not mocks):
  1. Full CRUD happy path: create a page from an owned asset, list it back
     with a resolved image_url, rename/replace/deactivate it, reorder a
     set of pages, delete one.
  2. Tenant isolation: business B cannot attach business A's asset to a
     page, cannot see/update/reorder/delete business A's pages, and a
     reorder request mixing in another tenant's page id silently ignores
     that entry rather than leaking or corrupting it.
  3. Backward compatibility: creating/deleting pages never touches
     businesses.menu_image_url — a single-image business's existing field
     is byte-for-byte unchanged by this feature's existence.

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
Skips cleanly if the dedicated test Postgres is configured but temporarily
unreachable; same pattern as test_business_list_search.py.
"""
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from backend.app.api.v1.routes_menu_images import (
    MenuImageCreate,
    MenuImageReorderEntry,
    MenuImageReorderRequest,
    MenuImageUpdate,
    create_menu_image,
    delete_menu_image,
    list_menu_images,
    reorder_menu_images,
    update_menu_image,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.asset import Asset
from shared.models.business import Business
from shared.models.menu_image import BusinessMenuImage
from shared.utils import generate_business_code


def _owner_auth(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def _make_business(db, name: str) -> Business:
    biz = Business(name=name, slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
                    business_code=generate_business_code())
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    return biz


async def _make_asset(db, business_id: uuid.UUID, kind: str = "BUSINESS_MENU_IMAGE") -> Asset:
    asset = Asset(
        business_id=business_id, kind=kind,
        storage_path=f"{business_id}/{kind.lower()}/{uuid.uuid4()}.jpg",
        content_type="image/jpeg",
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


async def _cleanup(db, *, business_ids=()):
    await db.rollback()
    for bid in business_ids:
        await db.execute(delete(BusinessMenuImage).where(BusinessMenuImage.business_id == bid))
        await db.execute(delete(Asset).where(Asset.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    await db.commit()


@pytest.fixture
async def _db_or_skip(db):
    try:
        await db.execute(select(1))
    except OperationalError:
        pytest.skip("Dedicated test Postgres not reachable — see TEST_DATABASE_URL / docker-compose.test.yml")
    return db


async def test_full_crud_happy_path_and_backward_compat(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    biz = await _make_business(db, f"Mochachos Test {unique}")
    auth = _owner_auth(biz.id)
    # Backward-compat baseline: existing single-image field is untouched.
    biz.menu_image_url = "https://example.test/original-menu.jpg"
    await db.commit()
    original_menu_image_url = biz.menu_image_url

    try:
        asset1 = await _make_asset(db, biz.id)
        asset2 = await _make_asset(db, biz.id)

        created = await create_menu_image(
            MenuImageCreate(asset_id=asset1.id, name="Regular Menu — Page 1"), user=auth, db=db,
        )
        assert created.sort_order == 0
        assert created.image_url and created.image_url.endswith(asset1.storage_path)

        created2 = await create_menu_image(
            MenuImageCreate(asset_id=asset2.id, name="Regular Menu — Page 2"), user=auth, db=db,
        )
        assert created2.sort_order == 1

        listing = await list_menu_images(user=auth, db=db)
        assert [p.name for p in listing] == ["Regular Menu — Page 1", "Regular Menu — Page 2"]

        renamed = await update_menu_image(
            created.id, MenuImageUpdate(name="Secret Creations"), user=auth, db=db,
        )
        assert renamed.name == "Secret Creations"

        deactivated = await update_menu_image(
            created2.id, MenuImageUpdate(is_active=False), user=auth, db=db,
        )
        assert deactivated.is_active is False

        reordered = await reorder_menu_images(
            MenuImageReorderRequest(order=[
                MenuImageReorderEntry(id=created.id, sort_order=5),
                MenuImageReorderEntry(id=created2.id, sort_order=1),
            ]),
            user=auth, db=db,
        )
        by_id = {p.id: p for p in reordered}
        assert by_id[created.id].sort_order == 5
        assert by_id[created2.id].sort_order == 1

        await delete_menu_image(created2.id, user=auth, db=db)
        remaining = await list_menu_images(user=auth, db=db)
        assert [p.id for p in remaining] == [created.id]

        # Backward compatibility: businesses.menu_image_url never touched
        # by any of the above.
        await db.refresh(biz)
        assert biz.menu_image_url == original_menu_image_url
    finally:
        await _cleanup(db, business_ids=[biz.id])


async def test_tenant_isolation_cannot_attach_another_businesss_asset(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    biz_a = await _make_business(db, f"Tenant A {unique}")
    biz_b = await _make_business(db, f"Tenant B {unique}")
    try:
        asset_a = await _make_asset(db, biz_a.id)
        auth_b = _owner_auth(biz_b.id)

        with pytest.raises(NotFoundError):
            await create_menu_image(
                MenuImageCreate(asset_id=asset_a.id, name="Stolen Page"), user=auth_b, db=db,
            )
    finally:
        await _cleanup(db, business_ids=[biz_a.id, biz_b.id])


async def test_tenant_isolation_cannot_see_update_or_delete_another_businesss_page(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    biz_a = await _make_business(db, f"Tenant A {unique}")
    biz_b = await _make_business(db, f"Tenant B {unique}")
    try:
        asset_a = await _make_asset(db, biz_a.id)
        auth_a = _owner_auth(biz_a.id)
        auth_b = _owner_auth(biz_b.id)

        page_a = await create_menu_image(
            MenuImageCreate(asset_id=asset_a.id, name="A's Menu"), user=auth_a, db=db,
        )

        # B cannot list A's page.
        listing_b = await list_menu_images(user=auth_b, db=db)
        assert listing_b == []

        # B cannot update A's page.
        with pytest.raises(NotFoundError):
            await update_menu_image(page_a.id, MenuImageUpdate(name="Hijacked"), user=auth_b, db=db)

        # B cannot delete A's page.
        with pytest.raises(NotFoundError):
            await delete_menu_image(page_a.id, user=auth_b, db=db)

        # A reorder call from B naming A's page id silently ignores it
        # rather than erroring or applying it.
        result_b = await reorder_menu_images(
            MenuImageReorderRequest(order=[MenuImageReorderEntry(id=page_a.id, sort_order=99)]),
            user=auth_b, db=db,
        )
        assert result_b == []
        page_a_row = await db.get(BusinessMenuImage, page_a.id)
        assert page_a_row.sort_order == 0  # untouched by B's reorder attempt

        # A can still see/manage its own page untouched.
        listing_a = await list_menu_images(user=auth_a, db=db)
        assert len(listing_a) == 1 and listing_a[0].id == page_a.id
    finally:
        await _cleanup(db, business_ids=[biz_a.id, biz_b.id])


async def test_max_pages_ceiling_enforced(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    biz = await _make_business(db, f"Max Pages {unique}")
    auth = _owner_auth(biz.id)
    try:
        from backend.app.api.v1.routes_menu_images import MAX_PAGES_PER_BUSINESS
        for i in range(MAX_PAGES_PER_BUSINESS):
            asset = await _make_asset(db, biz.id)
            await create_menu_image(MenuImageCreate(asset_id=asset.id, name=f"Page {i}"), user=auth, db=db)

        one_too_many = await _make_asset(db, biz.id)
        with pytest.raises(AppError):
            await create_menu_image(
                MenuImageCreate(asset_id=one_too_many.id, name="One too many"), user=auth, db=db,
            )
    finally:
        await _cleanup(db, business_ids=[biz.id])
