"""
Regression coverage for a production bug report: a recently-onboarded
restaurant was missing from the Admin Dashboard view.

Root cause (confirmed by reading the code, not guessed): `GET
/admin/businesses` (routes_businesses.py::list_businesses) sorts strictly by
`Business.created_at` -- the row's INSERT time, which is set once and never
changes (see shared/models/base.py::TimestampMixin) -- and paginates with a
default `per_page` of 20/25. A business whose onboarding took a while (its
row was created early, well before handover) can be pushed past page 1 by
every other business created since, with no way to search past the current
page: the admin suite's search box used to filter ONLY the single
already-fetched page client-side (see nextgen-admin-suite-main's
BusinessesPage.tsx, fixed alongside this backend change). The combined
effect: a real business, correctly stored and correctly visible to
SUPER_ADMIN in principle, reads as "missing" to an admin who searches for it
by name.

The fix adds a server-side `search` parameter (name/slug/business_code
substring, case-insensitive) applied BEFORE pagination, on top of the
existing scope filters -- never replacing or weakening them. This file
proves:
  1. A SUPER_ADMIN can find a business via `search` regardless of who
     onboarded it or whether onboarding is complete, AND regardless of
     which page it would otherwise land on (the actual bug mechanism).
  2. An ONBOARDER's `search` results stay confined to businesses scoped to
     them -- the new parameter must never be usable to discover a business
     outside that admin's existing allow-list.
  3. Without `search` (page=1, default per_page), the same old-row business
     is genuinely absent from page 1 once enough newer businesses exist --
     proving the bug would have reproduced before the fix, and that the fix
     doesn't just paper over the symptom.

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
Skips cleanly if the dedicated test Postgres is configured but temporarily
unreachable; same pattern as test_workflow_onboarding.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from admin_api.app.api.v1.routes_businesses import BusinessCreate, create_business, list_businesses
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.utils import generate_business_code


def _admin_auth(admin_id: uuid.UUID, role: str) -> AuthUser:
    return AuthUser(user_id=admin_id, business_id=None, role=role, token_type="admin_access")


async def _make_admin(db, role: str, email: str) -> AdminUser:
    admin = AdminUser(
        email=email, password_hash=hash_password("Irrelevant123!"),
        display_name="Test Admin", role=role, is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def _cleanup(db, *, business_ids=(), admin_ids=()):
    await db.rollback()
    for bid in business_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    for aid in admin_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == aid))
        await db.execute(delete(AdminUser).where(AdminUser.id == aid))
    await db.commit()


@pytest.fixture
async def _db_or_skip(db):
    try:
        await db.execute(select(1))
    except OperationalError:
        pytest.skip("Dedicated test Postgres not reachable — see TEST_DATABASE_URL / docker-compose.test.yml")
    return db


async def test_search_finds_a_business_regardless_of_onboarder_or_completion_state(_db_or_skip):
    """SUPER_ADMIN must find a business by name/slug/code via `search`
    whether it was created by a SUPER_ADMIN or an ONBOARDER, and whether or
    not that ONBOARDER's onboarding is complete -- `search` sits on top of
    the existing visibility rule, it does not change it for SUPER_ADMIN
    (who already sees everything)."""
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-search-{unique}@test.co.za")
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-search-{unique}@test.co.za")
    auth_onboarder = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)

    business_ids, admin_ids = [], [onboarder.id, super_admin.id]
    try:
        name_unfinished = f"Mama Thandis Kitchen {unique}"
        unfinished = await create_business(
            body=BusinessCreate(name=name_unfinished), user=auth_onboarder, db=db,
        )
        business_ids.append(unfinished.id)
        assert (await db.get(Business, unfinished.id)).onboarding_completed_at is None

        name_finished = f"Handed Over Diner {unique}"
        finished = await create_business(
            body=BusinessCreate(name=name_finished), user=auth_onboarder, db=db,
        )
        business_ids.append(finished.id)
        biz_row = await db.get(Business, finished.id)
        biz_row.onboarding_completed_at = datetime.now(timezone.utc)
        await db.commit()

        name_super_made = f"SuperAdmin Direct {unique}"
        super_made = await create_business(
            body=BusinessCreate(name=name_super_made), user=auth_super, db=db,
        )
        business_ids.append(super_made.id)

        for target, term in (
            (unfinished, name_unfinished),
            (finished, name_finished),
            (super_made, name_super_made),
        ):
            results = await list_businesses(
                search=term, page=1, per_page=25, user=auth_super, db=db,
            )
            assert any(b.id == target.id for b in results), (
                f"SUPER_ADMIN search for {term!r} did not find business {target.id}"
            )
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_onboarder_search_stays_confined_to_their_own_scope(_db_or_skip):
    """The new `search` parameter must never let an ONBOARDER find a
    business outside their existing allow-list (someone else's client, or a
    business they onboarded that has since been handed over)."""
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    owning_onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-owns-search-{unique}@test.co.za")
    other_onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-other-search-{unique}@test.co.za")
    auth_owning = _admin_auth(owning_onboarder.id, AdminRole.ONBOARDER)
    auth_other = _admin_auth(other_onboarder.id, AdminRole.ONBOARDER)

    business_ids, admin_ids = [], [owning_onboarder.id, other_onboarder.id]
    try:
        own_business = await create_business(
            body=BusinessCreate(name=f"Owning Onboarder Client {unique}"), user=auth_owning, db=db,
        )
        business_ids.append(own_business.id)

        others_business = await create_business(
            body=BusinessCreate(name=f"Other Onboarder Client {unique}"), user=auth_other, db=db,
        )
        business_ids.append(others_business.id)

        # `other_onboarder` searching broadly (empty-ish shared unique suffix
        # alone, matching both businesses by name) must still see ONLY their
        # own client.
        results = await list_businesses(search=unique, page=1, per_page=25, user=auth_other, db=db)
        result_ids = {b.id for b in results}
        assert others_business.id in result_ids
        assert own_business.id not in result_ids

        # Searching for the OTHER onboarder's business by its exact name
        # must return nothing for `owning_onboarder` -- confirms the 404-not-
        # 403 non-revealing pattern extends to search (empty list, not an
        # error, not a leak).
        results2 = await list_businesses(
            search=f"Other Onboarder Client {unique}", page=1, per_page=25, user=auth_owning, db=db,
        )
        assert not any(b.id == others_business.id for b in results2)

        # Once `owning_onboarder`'s own business is handed over, it drops out
        # of even THEIR search results (matches existing get_business_scoped
        # semantics -- onboarding_completed_at ends ONBOARDER visibility).
        biz_row = await db.get(Business, own_business.id)
        biz_row.onboarding_completed_at = datetime.now(timezone.utc)
        await db.commit()
        results3 = await list_businesses(
            search=f"Owning Onboarder Client {unique}", page=1, per_page=25, user=auth_owning, db=db,
        )
        assert not any(b.id == own_business.id for b in results3)
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_old_row_business_is_absent_from_default_page_but_found_by_search(_db_or_skip):
    """Proves the actual bug mechanism end-to-end: a business whose row is
    OLDER than enough other businesses is genuinely absent from page 1 of
    the default created_at-sorted, small-per_page list (reproducing what the
    platform owner saw) -- and that `search` is what actually finds it,
    rather than the fix being a no-op that happens to pass for an unrelated
    reason."""
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-oldrow-{unique}@test.co.za")
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)

    business_ids, admin_ids = [], [super_admin.id]
    try:
        # The target business: created well in the past (simulating a slow
        # onboarding), with a distinctive name.
        old = Business(
            name=f"Old Row Recently Onboarded {unique}",
            slug=f"old-row-recently-onboarded-{unique}",
            business_code=generate_business_code(),
            currency="ZAR", timezone="Africa/Johannesburg",
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        db.add(old)
        await db.commit()
        await db.refresh(old)
        business_ids.append(old.id)

        # 5 newer businesses, all created after `old`, enough to push it past
        # a small per_page page 1 when sorted created_at desc.
        for i in range(5):
            newer = await create_business(
                body=BusinessCreate(name=f"Newer Business {unique} {i}"), user=auth_super, db=db,
            )
            business_ids.append(newer.id)

        # Small page size, no search -- reproduces the bug: `old` is not on
        # page 1.
        page1 = await list_businesses(page=1, per_page=5, user=auth_super, db=db)
        assert not any(b.id == old.id for b in page1), (
            "test setup invariant failed: the old-row business must NOT be "
            "on an unfiltered page 1 for this test to actually prove anything"
        )

        # The fix: searching for it finds it immediately, regardless of page.
        found = await list_businesses(
            search=f"Old Row Recently Onboarded {unique}", page=1, per_page=5, user=auth_super, db=db,
        )
        assert any(b.id == old.id for b in found)
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)
