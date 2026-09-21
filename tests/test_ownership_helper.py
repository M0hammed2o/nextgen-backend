"""
Unit tests for backend/app/core/ownership.py's require_owned() — the
reusable tenant-ownership check introduced during the Wave 0A hardening
pass (2026-09) to stop routes from writing a client-submitted foreign key
(category_id, supplier_id, photo_asset_id, reference asset ids, ...) onto
a row without ever confirming it belongs to the caller's own business.

A mocked session cannot honestly prove a WHERE-clause tenant filter
actually filters at the database level — it just returns whatever canned
value the test wires up. These tests only prove require_owned()'s control
flow (found -> returns the object; not-found-or-foreign -> raises the same
NotFoundError either way) and that its query is built against both
`model.id` and `model.business_id`. The real cross-tenant proof, against a
live Postgres instance, lives in
tests/integration/test_tenant_isolation_writes.py.
"""
import uuid

import pytest

from backend.app.core.errors import NotFoundError
from backend.app.core.ownership import require_owned
from shared.models.menu import MenuCategory


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Records every statement passed to execute() and returns a canned result."""

    def __init__(self, return_value=None):
        self._return_value = return_value
        self.executed_statements = []

    async def execute(self, stmt):
        self.executed_statements.append(stmt)
        return FakeResult(self._return_value)


def _category(business_id: uuid.UUID) -> MenuCategory:
    return MenuCategory(id=uuid.uuid4(), business_id=business_id, name="Mains", sort_order=0)


async def test_require_owned_returns_the_row_when_found():
    business_id = uuid.uuid4()
    category = _category(business_id)
    db = FakeSession(return_value=category)

    result = await require_owned(db, MenuCategory, category.id, business_id, label="Category")

    assert result is category
    assert len(db.executed_statements) == 1


async def test_require_owned_raises_not_found_when_session_returns_nothing():
    """Covers both real cases this must collapse into one response: id doesn't
    exist at all, OR the id is real but belongs to a different business —
    require_owned's caller (the fake session) can't tell the two apart from
    the outside, which is exactly the point."""
    obj_id = uuid.uuid4()
    db = FakeSession(return_value=None)

    with pytest.raises(NotFoundError) as exc_info:
        await require_owned(db, MenuCategory, obj_id, uuid.uuid4(), label="Category")

    assert exc_info.value.status_code == 404
    assert str(obj_id) in exc_info.value.message
    assert exc_info.value.code == "CATEGORY_NOT_FOUND"


async def test_require_owned_query_filters_on_both_id_and_business_id():
    """
    Inspects the compiled query text to confirm the WHERE clause actually
    references both columns (plus any extra_filters) — not a substitute
    for the real-DB proof, but catches an accidental typo/regression in
    which column gets compared to what.
    """
    obj_id = uuid.uuid4()
    business_id = uuid.uuid4()
    db = FakeSession(return_value=None)

    with pytest.raises(NotFoundError):
        await require_owned(
            db, MenuCategory, obj_id, business_id, label="Category",
            extra_filters=(MenuCategory.is_active == True,),
        )

    assert len(db.executed_statements) == 1
    compiled = str(db.executed_statements[0])
    assert "menu_categories.id" in compiled
    assert "menu_categories.business_id" in compiled
    assert "menu_categories.is_active" in compiled


async def test_require_owned_does_not_distinguish_missing_from_foreign_in_the_raised_error():
    """
    Two different underlying situations (id never existed vs. id belongs to
    another business) must raise byte-for-byte the same NotFoundError, so a
    cross-tenant probe learns nothing from the response shape.
    """
    obj_id = uuid.uuid4()

    db_missing = FakeSession(return_value=None)
    db_foreign = FakeSession(return_value=None)  # the fake never actually applies the filter itself,
    # but require_owned's contract is: any None from the query means 404,
    # regardless of why the row didn't match.

    with pytest.raises(NotFoundError) as missing_exc:
        await require_owned(db_missing, MenuCategory, obj_id, uuid.uuid4(), label="Category")
    with pytest.raises(NotFoundError) as foreign_exc:
        await require_owned(db_foreign, MenuCategory, obj_id, uuid.uuid4(), label="Category")

    assert missing_exc.value.status_code == foreign_exc.value.status_code == 404
    assert missing_exc.value.code == foreign_exc.value.code
    assert missing_exc.value.message == foreign_exc.value.message
