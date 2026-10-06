"""
UI V2 restaurant branding — real Postgres.

Proves the branding layer added by migration `0053_restaurant_branding`:
persistence, the CHECK constraint, the trading-vs-legal name split, asset-URL
resolution, the permission boundary between the settings payload and the
narrow till-facing projection, and — most importantly — that branding cannot
cross a tenant boundary in either direction.

Real Postgres rather than a mocked session, for the usual reason: the two
properties most worth proving here (a database CHECK constraint, and a
WHERE-clause tenant filter) are exactly the two a mocked session cannot
honestly demonstrate. Uses the dedicated disposable environment described in
TESTING.md, resolved through tests/integration/_db.py.

Run directly with:
    docker compose -f docker-compose.test.yml up -d
    export TEST_DATABASE_URL=...   # see TESTING.md
    python -m alembic upgrade head
    python -m pytest tests/integration/test_restaurant_branding_realdb.py -q
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration._db import dispose_engine, get_engine, get_session_factory
from tests.integration._env import DedicatedTestEnvironmentNotConfigured, get_test_database_url

from backend.app.api.v1.routes_business import (
    BusinessSettingsUpdate,
    get_business_branding,
    get_business_settings,
    update_business_settings,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models import Asset, AuditEvent, Business
from shared.utils import generate_business_code

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db():
    try:
        get_test_database_url()
    except DedicatedTestEnvironmentNotConfigured as exc:
        pytest.skip(str(exc))
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"dedicated test database unreachable: {exc}")
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
    await dispose_engine()


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Branding Test {unique}",
        slug=f"branding-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        **overrides,
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _make_asset(db: AsyncSession, business_id: uuid.UUID, kind: str = "BUSINESS_LOGO") -> Asset:
    asset = Asset(
        business_id=business_id,
        kind=kind,
        storage_path=f"{business_id}/branding/{uuid.uuid4().hex}.png",
        content_type="image/png",
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


async def _cleanup(db: AsyncSession, business_id: uuid.UUID) -> None:
    await db.execute(delete(Asset).where(Asset.business_id == business_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
    await db.execute(delete(Business).where(Business.id == business_id))
    await db.commit()


@pytest.fixture
async def two_businesses(db: AsyncSession):
    a = await _make_business(db)
    b = await _make_business(db)
    # Capture the ids NOW. Reading `a.id` in the finally block after a test has
    # expired the instance triggers a lazy refresh outside the async context,
    # which surfaces as MissingGreenlet and masks the real test failure.
    a_id, b_id = a.id, b.id
    try:
        yield a, b
    finally:
        try:
            await _cleanup(db, a_id)
            await _cleanup(db, b_id)
        except Exception:
            await db.rollback()
            await _cleanup(db, a_id)
            await _cleanup(db, b_id)


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _staff(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")


# ── schema / persistence ─────────────────────────────────────────────────────


async def test_branding_defaults_are_unbranded_and_light(db: AsyncSession, two_businesses):
    """A restaurant that has configured nothing must be a clean 'no branding' state."""
    biz, _ = two_businesses
    await db.refresh(biz)
    assert biz.brand_primary_colour is None
    assert biz.brand_accent_colour is None
    assert biz.brand_display_name is None
    assert biz.logo_asset_id is None
    assert biz.brand_icon_asset_id is None
    # Light is the intended default experience, enforced by a server default
    # rather than left NULL for the UI to interpret.
    assert biz.brand_theme == "LIGHT"


async def test_brand_theme_check_constraint_rejects_anything_else(db: AsyncSession, two_businesses):
    """The database itself refuses an invalid theme, not just the API layer."""
    biz_id = two_businesses[0].id
    with pytest.raises(IntegrityError):
        await db.execute(
            text("UPDATE businesses SET brand_theme = 'NEON' WHERE id = :i"), {"i": str(biz_id)}
        )
        await db.commit()
    # A failed statement leaves the transaction aborted; every later statement
    # on this session fails until it is rolled back.
    await db.rollback()

    for valid in ("LIGHT", "DARK", "SYSTEM"):
        await db.execute(
            text("UPDATE businesses SET brand_theme = :t WHERE id = :i"),
            {"t": valid, "i": str(biz_id)},
        )
        await db.commit()


# ── settings read/write ──────────────────────────────────────────────────────


async def test_owner_can_save_branding_and_read_it_back(db: AsyncSession, two_businesses):
    biz, _ = two_businesses
    logo = await _make_asset(db, biz.id)
    icon = await _make_asset(db, biz.id, kind="BUSINESS_LOGO")

    await update_business_settings(
        body=BusinessSettingsUpdate(
            brand_primary_colour="#C62828",
            brand_accent_colour="#F9A825",
            brand_display_name="NOOSH",
            brand_theme="DARK",
            logo_asset_id=logo.id,
            brand_icon_asset_id=icon.id,
        ),
        user=_owner(biz.id),
        db=db,
    )

    settings = await get_business_settings(user=_owner(biz.id), db=db)
    assert settings.brand_primary_colour == "#C62828"
    assert settings.brand_accent_colour == "#F9A825"
    assert settings.brand_display_name == "NOOSH"
    assert settings.brand_theme == "DARK"
    assert settings.logo_asset_id == logo.id
    assert settings.brand_icon_asset_id == icon.id
    # Asset ids are resolved to displayable URLs so the dashboard never has to
    # know how a Supabase Storage path is built.
    assert settings.logo_url and logo.storage_path in settings.logo_url
    assert settings.brand_icon_url and icon.storage_path in settings.brand_icon_url


async def test_malformed_colours_are_refused_before_they_reach_the_database(db: AsyncSession, two_businesses):
    """
    A colour is stored exactly as a picker emits it. Anything looser would have
    to be normalised somewhere, and 'somewhere' becomes two places.
    """
    biz, _ = two_businesses
    for bad in ("red", "C62828", "#C628", "#GGGGGG", "rgb(1,2,3)", "#c62828ff"):
        with pytest.raises(Exception):
            BusinessSettingsUpdate(brand_primary_colour=bad)

    # ...and the valid forms a picker really produces are accepted.
    for good in ("#C62828", "#c62828", "#000000", "#FFFFFF"):
        assert BusinessSettingsUpdate(brand_primary_colour=good).brand_primary_colour == good


async def test_invalid_theme_is_refused_by_the_schema(db: AsyncSession, two_businesses):
    for bad in ("neon", "light", "Dark", "SYSTEM "):
        with pytest.raises(Exception):
            BusinessSettingsUpdate(brand_theme=bad)
    for good in ("LIGHT", "DARK", "SYSTEM"):
        assert BusinessSettingsUpdate(brand_theme=good).brand_theme == good


# ── tenant isolation ─────────────────────────────────────────────────────────


async def test_cannot_point_branding_at_another_restaurants_asset(db: AsyncSession, two_businesses):
    """
    The whole reason logo_asset_id/brand_icon_asset_id were added to
    _ASSET_REFERENCE_FIELDS. Without that, restaurant A could display
    restaurant B's logo by guessing an id.
    """
    biz_a, biz_b = two_businesses
    # Capture ids up front: a rollback inside the loop expires these instances,
    # and reading `.id` afterwards would lazy-load outside the async context.
    a_id, b_id = biz_a.id, biz_b.id
    b_logo = await _make_asset(db, b_id)
    b_logo_id = b_logo.id

    for field in ("logo_asset_id", "brand_icon_asset_id"):
        with pytest.raises(AppError) as exc:
            await update_business_settings(
                body=BusinessSettingsUpdate(**{field: b_logo_id}),
                user=_owner(a_id),
                db=db,
            )
        # require_owned raises NotFoundError (404) rather than 403 on purpose:
        # never confirm to a caller that another tenant's object exists.
        assert exc.value.status_code in (403, 404)
        await db.rollback()

    refreshed = (await db.execute(select(Business).where(Business.id == a_id))).scalar_one()
    assert refreshed.logo_asset_id is None
    assert refreshed.brand_icon_asset_id is None


async def test_branding_endpoint_only_ever_returns_the_callers_own_restaurant(
    db: AsyncSession, two_businesses
):
    biz_a, biz_b = two_businesses
    await update_business_settings(
        body=BusinessSettingsUpdate(brand_primary_colour="#C62828", brand_display_name="A Diner"),
        user=_owner(biz_a.id),
        db=db,
    )
    await update_business_settings(
        body=BusinessSettingsUpdate(brand_primary_colour="#1565C0", brand_display_name="B Cafe"),
        user=_owner(biz_b.id),
        db=db,
    )

    a = await get_business_branding(user=_staff(biz_a.id), db=db)
    b = await get_business_branding(user=_staff(biz_b.id), db=db)

    assert a.business_id == biz_a.id and a.primary_colour == "#C62828" and a.display_name == "A Diner"
    assert b.business_id == biz_b.id and b.primary_colour == "#1565C0" and b.display_name == "B Cafe"
    # Scoped by the JWT's business_id and nothing the client sends.
    assert a.primary_colour != b.primary_colour


async def test_a_dangling_asset_reference_degrades_instead_of_500ing(db: AsyncSession, two_businesses):
    """
    A missing logo must fall back to the monogram, never break the settings
    page. Writes cannot create this state, but a deleted asset row can.
    """
    biz, _ = two_businesses
    logo = await _make_asset(db, biz.id)
    await update_business_settings(
        body=BusinessSettingsUpdate(logo_asset_id=logo.id), user=_owner(biz.id), db=db
    )
    await db.execute(delete(Asset).where(Asset.id == logo.id))
    await db.commit()

    settings = await get_business_settings(user=_owner(biz.id), db=db)
    assert settings.logo_asset_id == logo.id
    assert settings.logo_url is None  # degraded, not raised

    branding = await get_business_branding(user=_staff(biz.id), db=db)
    assert branding.logo_url is None


# ── the till-facing projection ───────────────────────────────────────────────


async def test_branding_is_readable_by_staff_while_full_settings_are_not(
    db: AsyncSession, two_businesses
):
    """
    The reason /branding exists at all: a till needs its restaurant's identity,
    but a cashier's token must not be able to read payment routing, credential
    hints, VAT configuration or entitlement flags.

    The permission boundary is asserted against the DECLARED ROUTE
    DEPENDENCIES, not by calling the handlers. Calling a handler directly (as
    every other test in this file does, to exercise real SQL) bypasses
    FastAPI's dependency injection entirely, so `Depends(require_owner_or_manager)`
    never runs and a direct call would "prove" a boundary that is not being
    tested at all. Inspecting the wiring is the honest check.
    """
    from backend.app.core.rbac import require_owner_or_manager, require_staff_or_above
    from backend.app.main import app

    def _guards(path: str) -> set:
        route = next(r for r in app.routes if getattr(r, "path", None) == path)
        return {d.call for d in route.dependant.dependencies}

    branding_guards = _guards("/v1/business/branding")
    settings_guards = _guards("/v1/business/settings")

    # The till can read branding...
    assert require_staff_or_above in branding_guards
    # ...and cannot read the full settings payload.
    assert require_owner_or_manager in settings_guards
    assert require_staff_or_above not in settings_guards

    # And the projection carries ONLY identity -- no payment/VAT/entitlement
    # data could leak even if the gate were ever widened by mistake.
    biz_id = two_businesses[0].id
    branding = await get_business_branding(user=_staff(biz_id), db=db)
    assert branding.business_id == biz_id
    leaked = set(branding.model_dump().keys()) & {
        "payment_api_key_hint",
        "payment_api_secret_hint",
        "payment_webhook_secret_configured",
        "vat_number",
        "inventory_enabled",
        "loyalty_enabled",
        "monthly_orders_used",
    }
    assert leaked == set()


async def test_display_name_falls_back_to_the_legal_name_and_never_overwrites_it(
    db: AsyncSession, two_businesses
):
    """
    `businesses.name` is billed on NextGen's own commercial invoices, so the
    trading name shown on the till must be a separate field, not a rename.
    """
    biz, _ = two_businesses
    legal = biz.name

    before = await get_business_branding(user=_staff(biz.id), db=db)
    assert before.display_name == legal  # no trading name yet -> falls back
    assert before.legal_name == legal

    await update_business_settings(
        body=BusinessSettingsUpdate(brand_display_name="NOOSH"), user=_owner(biz.id), db=db
    )

    after = await get_business_branding(user=_staff(biz.id), db=db)
    assert after.display_name == "NOOSH"
    assert after.legal_name == legal  # untouched — the invoice still bills this

    await db.refresh(biz)
    assert biz.name == legal
