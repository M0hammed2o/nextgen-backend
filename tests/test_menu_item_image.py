"""
Menu item image resolution (2026-07-27): `image_asset_id` submitted via
ItemCreate/ItemUpdate was previously never resolved into `image_url` —
an owner could attach an image and never see it render anywhere, since
ItemResponse.image_url stayed null forever. Covers the new
`_resolve_item_image` helper (routes_menu.py) directly, plus its wiring
into `create_item`/`update_item`.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.v1 import routes_menu
from backend.app.api.v1.routes_menu import (
    ItemCreate,
    ItemUpdate,
    _resolve_item_image,
    create_item,
    update_item,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.asset import Asset
from shared.models.menu import MenuItem


def make_user(role="OWNER", business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_asset(business_id, storage_path="biz/menu_item_image/abc.jpg"):
    return Asset(
        id=uuid.uuid4(),
        business_id=business_id,
        kind="MENU_ITEM_IMAGE",
        storage_path=storage_path,
        content_type="image/jpeg",
    )


class FakeGetDB:
    """Minimal fake supporting only db.get(Model, id), for _resolve_item_image tests."""

    def __init__(self, get_return=None):
        self._get_return = get_return
        self.get_calls = []

    async def get(self, model, id_):
        self.get_calls.append((model, id_))
        return self._get_return


# ── _resolve_item_image ──────────────────────────────────────────────────────

async def test_resolve_item_image_returns_none_when_no_asset_id():
    db = FakeGetDB(get_return=None)
    url, asset = await _resolve_item_image(db, uuid.uuid4(), None)
    assert url is None
    assert asset is None
    assert db.get_calls == []  # must not query when nothing was submitted


async def test_resolve_item_image_rejects_missing_asset():
    db = FakeGetDB(get_return=None)
    with pytest.raises(AppError) as exc:
        await _resolve_item_image(db, uuid.uuid4(), uuid.uuid4())
    assert exc.value.status_code == 404


async def test_resolve_item_image_rejects_foreign_business_asset():
    """Tenant isolation: an asset_id belonging to another business must never resolve."""
    owner_business_id = uuid.uuid4()
    other_business_id = uuid.uuid4()
    foreign_asset = make_asset(business_id=other_business_id)
    db = FakeGetDB(get_return=foreign_asset)
    with pytest.raises(AppError) as exc:
        await _resolve_item_image(db, owner_business_id, foreign_asset.id)
    assert exc.value.status_code == 404


async def test_resolve_item_image_resolves_url_for_owned_asset():
    business_id = uuid.uuid4()
    asset = make_asset(business_id=business_id, storage_path="biz/menu_item_image/xyz.png")
    db = FakeGetDB(get_return=asset)
    with patch.object(routes_menu, "asset_public_url", return_value="https://cdn.example/xyz.png") as mock_url:
        url, returned_asset = await _resolve_item_image(db, business_id, asset.id)
    mock_url.assert_called_once_with(asset.storage_path)
    assert url == "https://cdn.example/xyz.png"
    assert returned_asset is asset


# ── create_item wiring ───────────────────────────────────────────────────────

class FakeCreateDB:
    def __init__(self, currency_scalar="ZAR", business=None):
        self._currency_scalar = currency_scalar
        self._business = business
        self.added = []
        self.flushed = False
        self.committed = False
        self.refreshed = []

    async def execute(self, stmt):
        class _Result:
            def __init__(self, val):
                self._val = val

            def scalar(self):
                return self._val
        return _Result(self._currency_scalar)

    async def get(self, model, id_):
        return self._business

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        self.flushed = True
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        self.refreshed.append(obj)
        # Simulate the real DB applying column defaults on INSERT, then
        # being reloaded via refresh() — a plain transient MenuItem()
        # never gets these applied without a real engine.
        if obj.is_active is None:
            obj.is_active = True
        if obj.is_deleted is None:
            obj.is_deleted = False


class FakeBusiness:
    def __init__(self, currency="ZAR"):
        self.currency = currency


async def test_create_item_resolves_and_sets_image_url():
    business_id = uuid.uuid4()
    user = make_user(business_id=business_id)
    asset = make_asset(business_id=business_id)
    body = ItemCreate(name="Burger", price_cents=5000, image_asset_id=asset.id)
    db = FakeCreateDB(business=FakeBusiness())

    with patch.object(
        routes_menu, "_resolve_item_image",
        AsyncMock(return_value=("https://cdn.example/burger.jpg", asset)),
    ):
        result = await create_item(body=body, user=user, db=db)

    assert result.image_url == "https://cdn.example/burger.jpg"
    assert asset.entity_id == result.id  # backfilled once the item's id exists
    assert db.committed


async def test_create_item_without_image_asset_id_leaves_image_url_none():
    user = make_user()
    body = ItemCreate(name="Fries", price_cents=2000)
    db = FakeCreateDB(business=FakeBusiness())

    with patch.object(
        routes_menu, "_resolve_item_image",
        AsyncMock(return_value=(None, None)),
    ):
        result = await create_item(body=body, user=user, db=db)

    assert result.image_url is None


# ── update_item wiring ───────────────────────────────────────────────────────

class FakeUpdateDB:
    def __init__(self, item):
        self._item = item
        self.committed = False

    async def execute(self, stmt):
        class _Result:
            def __init__(self, item):
                self._item = item

            def scalar_one_or_none(self):
                return self._item
        return _Result(self._item)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


def make_existing_item(business_id, image_url=None):
    return MenuItem(
        id=uuid.uuid4(),
        business_id=business_id,
        name="Existing Item",
        price_cents=3000,
        currency="ZAR",
        is_active=True,
        is_deleted=False,
        sort_order=0,
        image_url=image_url,
        requires_preparation=True,
    )


async def test_update_item_resolves_new_image_when_asset_id_submitted():
    business_id = uuid.uuid4()
    user = make_user(business_id=business_id)
    item = make_existing_item(business_id, image_url=None)
    asset = make_asset(business_id=business_id)
    db = FakeUpdateDB(item)

    with patch.object(
        routes_menu, "_resolve_item_image",
        AsyncMock(return_value=("https://cdn.example/new.jpg", asset)),
    ) as mock_resolve:
        body = ItemUpdate(image_asset_id=asset.id)
        result = await update_item(item_id=item.id, body=body, user=user, db=db)

    mock_resolve.assert_called_once_with(db, business_id, asset.id)
    assert result.image_url == "https://cdn.example/new.jpg"
    assert asset.entity_id == item.id  # backfilled on update too


async def test_update_item_without_image_field_leaves_existing_image_url_untouched():
    business_id = uuid.uuid4()
    user = make_user(business_id=business_id)
    item = make_existing_item(business_id, image_url="https://cdn.example/original.jpg")
    db = FakeUpdateDB(item)

    with patch.object(routes_menu, "_resolve_item_image", AsyncMock()) as mock_resolve:
        body = ItemUpdate(name="Renamed Item")  # image_asset_id not set at all
        result = await update_item(item_id=item.id, body=body, user=user, db=db)

    mock_resolve.assert_not_called()
    assert result.image_url == "https://cdn.example/original.jpg"
    assert result.name == "Renamed Item"
