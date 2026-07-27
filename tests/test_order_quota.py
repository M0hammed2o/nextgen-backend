"""
Monthly order-quota computation (backend/app/billing/usage.py) — the
"1,500 Included Customer Orders per Month" + Order Pack pricing model.
Deliberately non-enforcing (see module docstring) — these tests only cover
the computation, not any blocking behavior, because none exists yet.
"""
import types
import uuid
from datetime import datetime, timezone

from backend.app.billing.usage import get_order_quota_status


class FakeResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar


class FakeDB:
    def __init__(self, used_total):
        self._used_total = used_total

    async def execute(self, stmt):
        return FakeResult(self._used_total)


def make_business(**overrides):
    b = types.SimpleNamespace(
        id=uuid.uuid4(),
        monthly_order_quota=1500,
        order_pack_credits=0,
        billing_period_started_at=datetime.now(timezone.utc),
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


async def test_quota_status_with_no_usage():
    business = make_business()
    db = FakeDB(used_total=0)
    status = await get_order_quota_status(db, business)
    assert status.quota == 1500
    assert status.used == 0
    assert status.remaining == 1500


async def test_quota_status_with_partial_usage():
    business = make_business()
    db = FakeDB(used_total=1200)
    status = await get_order_quota_status(db, business)
    assert status.remaining == 300


async def test_quota_status_remaining_never_goes_negative():
    business = make_business()
    db = FakeDB(used_total=2000)  # over quota
    status = await get_order_quota_status(db, business)
    assert status.remaining == 0


async def test_order_pack_credits_extend_the_allowance():
    business = make_business(order_pack_credits=500)
    db = FakeDB(used_total=1600)
    status = await get_order_quota_status(db, business)
    # 1500 + 500 = 2000 allowance, 1600 used -> 400 remaining
    assert status.remaining == 400


async def test_null_usage_sum_treated_as_zero():
    """func.coalesce(...) means a business with zero DailyUsage rows still gets 0, not None."""
    business = make_business()
    db = FakeDB(used_total=0)
    status = await get_order_quota_status(db, business)
    assert status.used == 0
