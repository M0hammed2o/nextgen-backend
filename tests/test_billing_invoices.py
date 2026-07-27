"""
GET /business/billing/invoices — invoice history storage (backend/app/billing/
lifecycle.py writes these on every provider webhook; PDF rendering and email
delivery are separate, unbuilt pieces — see TODO.md).
"""
import types
import uuid
from datetime import datetime, timezone

from backend.app.api.v1.routes_billing import list_invoices
from backend.app.core.rbac import AuthUser


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalars(self._items)


class FakeDB:
    def __init__(self, items):
        self._items = items

    async def execute(self, stmt):
        return FakeResult(self._items)


def make_user(business_id=None):
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role="OWNER", token_type="access")


def make_invoice(**overrides):
    inv = types.SimpleNamespace(
        id=uuid.uuid4(), provider="STRIPE", amount_cents=120000, status="PAID",
        plan="STARTER", issued_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(inv, k, v)
    return inv


async def test_list_invoices_returns_stored_records():
    user = make_user()
    invoices = [make_invoice(status="PAID"), make_invoice(status="FAILED", amount_cents=120000)]
    db = FakeDB(items=invoices)

    result = await list_invoices(user=user, db=db)

    assert len(result) == 2
    assert result[0].status == "PAID"
    assert result[0].amount_cents == 120000


async def test_list_invoices_empty_when_none_exist():
    user = make_user()
    db = FakeDB(items=[])

    result = await list_invoices(user=user, db=db)

    assert result == []
