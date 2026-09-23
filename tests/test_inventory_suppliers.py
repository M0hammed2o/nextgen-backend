"""
Supplier CRUD (Ciao Pizza gap-closure pass, 2026-09-23).

Before this change, routes_inventory.py only had create+list for suppliers
— no update, no way to reach the existing Supplier.is_active flag, and no
delete at all. These tests cover the new update/deactivate/reactivate/
delete endpoints and their invariants:

  - update edits fields and/or toggles is_active, audited via AuditEvent
    (same discipline as the pre-existing manual stock adjustment).
  - delete is refused (409) when the supplier has ANY goods receipt on
    record, regardless of what the DB's ON DELETE SET NULL would
    technically allow — losing that attribution is exactly the kind of
    silent historical corruption the check exists to prevent.
  - delete is refused (409) when the supplier is still active — mirrors
    the staff hard-delete precedent (deactivate first).
  - delete succeeds only when both gates pass.
  - list/response carry usage visibility (goods_receipt_count,
    last_received_at) so deactivation/deletion is an informed decision.
  - tenant isolation: a cross-tenant supplier id 404s (via require_owned),
    never leaking whether the id belongs to another business.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.api.v1.routes_inventory import (
    SupplierCreateRequest,
    SupplierUpdateRequest,
    create_supplier,
    delete_supplier,
    list_suppliers,
    update_supplier,
)
from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser
from shared.models.inventory import Supplier


class FakeResult:
    def __init__(self, scalar=None, rows=None, scalar_one=None):
        self._scalar = scalar
        self._rows = rows or []
        self._scalar_one = scalar_one

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar_one

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def unique(self):
        return self


class FakeDB:
    def __init__(self, results, commit_exc=None):
        self._results = list(results)
        self.added = []
        self.deleted = []
        self.commits = 0
        self.rollbacks = 0
        self._commit_exc = commit_exc
        self._commit_calls = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self._finalize(obj)
        self.added.append(obj)

    @staticmethod
    def _finalize(obj):
        now = datetime.now(timezone.utc)
        if hasattr(obj, "id") and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
            obj.created_at = now

    async def flush(self):
        pass

    async def commit(self):
        self._commit_calls += 1
        if self._commit_exc is not None and self._commit_calls == 1:
            raise self._commit_exc
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj):
        self._finalize(obj)

    async def delete(self, obj):
        self.deleted.append(obj)


def make_user(role="OWNER", business_id=None, user_id=None) -> AuthUser:
    return AuthUser(
        user_id=user_id or uuid.uuid4(),
        business_id=business_id or uuid.uuid4(),
        role=role,
        token_type="access",
    )


def make_business(business_id, inventory_enabled=True):
    import types
    return types.SimpleNamespace(id=business_id, inventory_enabled=inventory_enabled)


def make_supplier(business_id, is_active=True, name="ACME Supplies"):
    return Supplier(
        id=uuid.uuid4(), business_id=business_id, name=name,
        contact_name=None, phone=None, email=None, is_active=is_active,
    )


def make_integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError("INSERT INTO suppliers ...", {}, Exception(
        f'duplicate key value violates unique constraint "{constraint_name}"'
    ))


def _movements(db):
    return db.added


# ── list_suppliers: usage visibility ────────────────────────────────────────

async def test_list_suppliers_includes_usage_stats():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id)
    last_at = datetime.now(timezone.utc)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(rows=[supplier]),  # supplier list query
        FakeResult(rows=[(supplier.id, 3, last_at)]),  # usage aggregate
    ])

    result = await list_suppliers(include_inactive=False, user=user, db=db)

    assert len(result) == 1
    assert result[0].goods_receipt_count == 3
    assert result[0].last_received_at == last_at


async def test_list_suppliers_unused_supplier_shows_zero():
    user = make_user(role="STAFF")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(rows=[supplier]),
        FakeResult(rows=[]),  # no goods receipts at all
    ])

    result = await list_suppliers(include_inactive=False, user=user, db=db)

    assert result[0].goods_receipt_count == 0
    assert result[0].last_received_at is None


# ── create_supplier: duplicate name is a clean 409, not a 500 ──────────────

async def test_create_supplier_duplicate_name_is_409_not_500():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    db = FakeDB(
        [FakeResult(scalar=business)],
        commit_exc=make_integrity_error("uq_suppliers_business_id_name"),
    )

    with pytest.raises(DuplicateError):
        await create_supplier(
            body=SupplierCreateRequest(name="ACME Supplies"), user=user, db=db
        )
    assert db.rollbacks == 1


# ── update_supplier: edits + is_active toggle, audited ──────────────────────

async def test_update_supplier_edits_fields_and_audits():
    user = make_user(role="MANAGER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, name="Old Name")

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),  # require_owned lookup
        FakeResult(rows=[(supplier.id, 0, None)]),  # usage lookup
    ])

    result = await update_supplier(
        supplier_id=supplier.id,
        body=SupplierUpdateRequest(name="New Name", phone="0821234567"),
        user=user, db=db,
    )

    assert result.name == "New Name"
    assert result.phone == "0821234567"
    assert db.commits == 1
    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert len(audit_events) == 1
    assert audit_events[0].action == "inventory.supplier.updated"
    assert audit_events[0].diff_json["name"] == {"old": "Old Name", "new": "New Name"}


async def test_update_supplier_deactivate_toggles_is_active_and_audits():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, is_active=True)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),
        FakeResult(rows=[(supplier.id, 5, datetime.now(timezone.utc))]),
    ])

    result = await update_supplier(
        supplier_id=supplier.id,
        body=SupplierUpdateRequest(is_active=False),
        user=user, db=db,
    )

    assert result.is_active is False
    assert supplier.is_active is False
    assert result.goods_receipt_count == 5  # usage still visible after deactivation
    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert audit_events[0].diff_json["is_active"] == {"old": True, "new": False}


async def test_update_supplier_no_changes_does_not_commit_or_audit():
    """A PATCH with nothing actually different is a no-op — no spurious
    audit trail entry for a change that didn't happen."""
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, name="Same Name")

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),
        FakeResult(rows=[]),
    ])

    result = await update_supplier(
        supplier_id=supplier.id,
        body=SupplierUpdateRequest(name="Same Name"),
        user=user, db=db,
    )

    assert result.name == "Same Name"
    assert db.commits == 0
    assert db.added == []


async def test_update_supplier_cross_tenant_is_404():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    other_supplier_id = uuid.uuid4()

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=None),  # require_owned: no row for this business
    ])

    with pytest.raises(NotFoundError):
        await update_supplier(
            supplier_id=other_supplier_id,
            body=SupplierUpdateRequest(name="Hijack"),
            user=user, db=db,
        )


# ── delete_supplier: the two safety gates ───────────────────────────────────

async def test_delete_supplier_blocked_when_referenced_by_goods_receipts():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, is_active=False)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),
        FakeResult(scalar_one=2),  # 2 goods receipts reference this supplier
    ])

    with pytest.raises(AppError) as exc:
        await delete_supplier(supplier_id=supplier.id, user=user, db=db)
    assert exc.value.code == "SUPPLIER_IN_USE"
    assert exc.value.details["goods_receipt_count"] == 2
    assert db.deleted == []
    assert db.commits == 0


async def test_delete_supplier_blocked_when_still_active():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, is_active=True)  # never deactivated

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),
        FakeResult(scalar_one=0),  # never referenced
    ])

    with pytest.raises(AppError) as exc:
        await delete_supplier(supplier_id=supplier.id, user=user, db=db)
    assert exc.value.code == "SUPPLIER_STILL_ACTIVE"
    assert db.deleted == []


async def test_delete_supplier_succeeds_when_unused_and_inactive():
    user = make_user(role="OWNER")
    business = make_business(user.business_id)
    supplier = make_supplier(user.business_id, is_active=False)

    db = FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalar=supplier),
        FakeResult(scalar_one=0),
    ])

    result = await delete_supplier(supplier_id=supplier.id, user=user, db=db)

    assert result is None
    assert db.deleted == [supplier]
    assert db.commits == 1
    from shared.models.audit import AuditEvent
    audit_events = [o for o in db.added if isinstance(o, AuditEvent)]
    assert audit_events[0].action == "inventory.supplier.deleted"
