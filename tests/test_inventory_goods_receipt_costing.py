"""
Goods-receipt costing (BUG-0111 fix, 2026-09-25).

Before this fix, `POST /business/inventory/goods-receipts` had two
independent bugs in the same block:

  1. `line_cost_cents` was `int(line.unit_cost_cents or 0)` -- never
     multiplied by the quantity received, so 5 kg at R100/kg recorded
     R100.00 (the per-kg rate) instead of R500.00 (the actual total).
  2. The stock movement was *always* valued from the ingredient's standing
     cost (`ingredient.cost_per_canonical_micro_cents`), ignoring whatever
     cost the receipt itself supplied -- so the receipt header, the receipt
     line, and the ledger movement could show three different numbers for
     one delivery.

ESTABLISHED SEMANTICS (see DECISIONS.md "Goods-receipt cost semantics" and
DATABASE.md for the full write-up):
  - `unit_cost_cents` is the cost of ONE of the `unit` entered on that
    line (cents per kg, cents per litre, cents per whole pack, ...), not a
    line total. Total line cost = unit_cost_cents * quantity.
  - Ingredient cost is a STANDING cost, set explicitly via
    PATCH .../ingredients/{id} (or at creation), derived from pack cost.
    Receiving goods does NOT change it -- confirmed by grepping every
    write site of `cost_per_canonical_micro_cents` in the codebase; the
    only writers are ingredient create/update and the CSV importer, never
    a goods-receipt or stock-movement code path.
  - Where a receipt line supplies its own cost, the stock movement is
    valued from THAT cost (re-expressed as micro-cents per canonical unit
    via `cost_per_canonical_micro_cents()`), not the standing cost.
  - Where a receipt line omits cost (legal -- `unit_cost_cents` is
    optional), the movement (and the line/header) fall back to the
    ingredient's standing cost, explicitly, rather than recording a
    silent R0.00 against real stock value.

This file exercises `create_goods_receipt` directly (route-level unit
tests against a fake `AsyncSession`, matching the pattern used by
`tests/test_inventory_opening_stock.py`) since the wider suite is fully
mocked -- see TESTING.md. `tests/integration/` (real Postgres) is used
separately for the end-to-end database verification in this fix's
worklog, not committed as a permanent test here since the dedicated test
container is transient tooling, not part of the pytest suite.
"""
import types
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.api.v1.routes_inventory import (
    GoodsReceiptCreateRequest,
    GoodsReceiptLineRequest,
    create_goods_receipt,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.inventory import GoodsReceiptLine, StockMovement


# ── Fakes (mirrors tests/test_inventory_opening_stock.py) ──────────────────

class FakeResult:
    def __init__(self, scalar=None, scalars_list=None):
        self._scalar = scalar
        self._scalars_list = scalars_list

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_list or []

        class _Scalars:
            def all(_self):
                return items

        return _Scalars()


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

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
        self.commits += 1

    async def refresh(self, obj):
        self._finalize(obj)


def make_user(role="STAFF", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(),
                     role=role, token_type="access")


def make_business(business_id, inventory_enabled=True):
    return types.SimpleNamespace(id=business_id, inventory_enabled=inventory_enabled)


def make_ingredient(
    business_id,
    canonical_unit="GRAM",
    cost_micro_cents=50_000_000,  # R0.50/canonical unit standing cost
    purchase_pack_milli=1000,
):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=business_id,
        name="Mozzarella",
        canonical_unit=canonical_unit,
        unit_label=None,
        cost_per_canonical_micro_cents=cost_micro_cents,
        purchase_pack_milli=purchase_pack_milli,
        is_deleted=False,
    )


def lines_of(db):
    return [o for o in db.added if isinstance(o, GoodsReceiptLine)]


def movements_of(db):
    return [o for o in db.added if isinstance(o, StockMovement)]


def _fake_db_for(user, ingredients):
    business = make_business(user.business_id)
    return FakeDB([
        FakeResult(scalar=business),
        FakeResult(scalars_list=ingredients),
    ])


# ── Worked example: 5 kg at R100/kg = R500.00 ───────────────────────────────

async def test_5kg_at_r100_per_kg_is_r500_everywhere():
    user = make_user()
    ingredient = make_ingredient(user.business_id, canonical_unit="GRAM")
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="5", unit="KG",
                                 unit_cost_cents=10000),  # R100.00/kg
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 50000  # R500.00, not 10000 and not 110000

    line = lines_of(db)[0]
    assert line.quantity_milli == 5_000_000
    assert line.unit_cost_cents == 10000
    assert line.line_cost_cents == 50000

    movement = movements_of(db)[0]
    assert movement.quantity_milli == 5_000_000
    assert movement.value_cents == 50000  # matches the line and the header exactly
    # Movement valuation is derived from THIS receipt's cost (10 c/g), not
    # the ingredient's unrelated standing cost (50 c/g, which would give
    # 250_000c / R2500 if the bug were still present).
    assert movement.unit_cost_micro_cents == 10_000_000


# ── 500 g at an appropriate per-kg-derived rate ─────────────────────────────

async def test_500g_receipt_cost():
    user = make_user()
    ingredient = make_ingredient(user.business_id, canonical_unit="GRAM")
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="500", unit="G",
                                 unit_cost_cents=12),  # 12c/gram = R120/kg
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 6000  # 500 * 12c = R60.00
    assert lines_of(db)[0].line_cost_cents == 6000
    assert movements_of(db)[0].value_cents == 6000


# ── Decimal quantity (2.5 kg) ────────────────────────────────────────────────

async def test_decimal_quantity_2_5kg():
    user = make_user()
    ingredient = make_ingredient(user.business_id, canonical_unit="GRAM")
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="2.5", unit="KG",
                                 unit_cost_cents=3333),  # R33.33/kg -- doesn't divide evenly
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 8333  # round-half-up of 8332.5
    assert lines_of(db)[0].quantity_milli == 2_500_000
    # Line total and movement valuation must agree exactly, even though the
    # per-gram micro-cent rate does not divide the money back out evenly.
    assert movements_of(db)[0].value_cents == 8333


# ── Litres / millilitres ─────────────────────────────────────────────────────

async def test_litres_and_millilitres():
    user = make_user()
    oil = make_ingredient(user.business_id, canonical_unit="MILLILITRE", cost_micro_cents=0)
    db = _fake_db_for(user, [oil])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=oil.id, quantity="2", unit="L",
                                 unit_cost_cents=5000),  # R50/litre
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 10000  # 2 * R50 = R100.00
    assert lines_of(db)[0].quantity_milli == 2_000_000  # 2 L = 2000 mL


async def test_millilitres_direct():
    user = make_user()
    oil = make_ingredient(user.business_id, canonical_unit="MILLILITRE", cost_micro_cents=0)
    db = _fake_db_for(user, [oil])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=oil.id, quantity="750", unit="ML",
                                 unit_cost_cents=8),  # 8c/ml
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 6000  # 750 * 8c = R60.00


# ── Each / count-based ingredient ───────────────────────────────────────────

async def test_each_count_ingredient():
    user = make_user()
    cans = make_ingredient(user.business_id, canonical_unit="UNIT", cost_micro_cents=0)
    db = _fake_db_for(user, [cans])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=cans.id, quantity="24", unit="EACH",
                                 unit_cost_cents=899),  # R8.99/can
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 21576  # 24 * 899 = R215.76


# ── Purchase-unit / PACK conversion ─────────────────────────────────────────

async def test_pack_conversion_cost():
    user = make_user()
    # Pack = 2 kg = 2_000_000 milli-grams
    cheese = make_ingredient(
        user.business_id, canonical_unit="GRAM", cost_micro_cents=0,
        purchase_pack_milli=2_000_000,
    )
    db = _fake_db_for(user, [cheese])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=cheese.id, quantity="5", unit="PACK",
                                 unit_cost_cents=19000),  # R190.00 per 2 kg pack
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 95000  # 5 packs * R190 = R950.00
    line = lines_of(db)[0]
    assert line.quantity_milli == 10_000_000  # 5 * 2 kg = 10 kg = 10_000_000 mg
    movement = movements_of(db)[0]
    assert movement.value_cents == 95000
    # R190 / 2kg = R95/kg = 9.5 c/g = 9_500_000 micro-cents/g
    assert movement.unit_cost_micro_cents == 9_500_000


# ── Multiple lines in one receipt ───────────────────────────────────────────

async def test_multiple_lines_sum_independently():
    user = make_user()
    flour = make_ingredient(user.business_id, canonical_unit="GRAM", cost_micro_cents=0)
    oil = make_ingredient(user.business_id, canonical_unit="MILLILITRE", cost_micro_cents=0)
    db = _fake_db_for(user, [flour, oil])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=flour.id, quantity="10", unit="KG",
                                 unit_cost_cents=1500),  # R15/kg -> R150.00
        GoodsReceiptLineRequest(ingredient_id=oil.id, quantity="5", unit="L",
                                 unit_cost_cents=6000),  # R60/L -> R300.00
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["total_cost_cents"] == 45000  # R150 + R300
    receipt_lines = lines_of(db)
    assert len(receipt_lines) == 2
    by_ingredient = {ln.ingredient_id: ln for ln in receipt_lines}
    assert by_ingredient[flour.id].line_cost_cents == 15000
    assert by_ingredient[oil.id].line_cost_cents == 30000
    assert len(movements_of(db)) == 2


# ── Zero / negative invalid values ──────────────────────────────────────────

def test_negative_unit_cost_rejected_by_schema():
    with pytest.raises(ValidationError):
        GoodsReceiptLineRequest(ingredient_id=uuid.uuid4(), quantity="5", unit="KG",
                                 unit_cost_cents=-100)


async def test_zero_quantity_rejected():
    user = make_user()
    ingredient = make_ingredient(user.business_id, canonical_unit="GRAM")
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="0", unit="KG",
                                 unit_cost_cents=1000),
    ])
    with pytest.raises(AppError) as exc:
        await create_goods_receipt(body=body, user=user, db=db)
    assert exc.value.code == "INVALID_QUANTITY"
    assert db.commits == 0


async def test_negative_quantity_rejected():
    user = make_user()
    ingredient = make_ingredient(user.business_id, canonical_unit="GRAM")
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="-5", unit="KG",
                                 unit_cost_cents=1000),
    ])
    with pytest.raises(AppError) as exc:
        await create_goods_receipt(body=body, user=user, db=db)
    assert exc.value.code == "INVALID_QUANTITY"


# ── Cross-dimension unit error ──────────────────────────────────────────────

async def test_cross_dimension_unit_rejected():
    """KG cost/quantity against a millilitre (volume) ingredient must be
    rejected before any cost math runs -- mass and volume are not
    interchangeable regardless of what number is typed."""
    user = make_user()
    oil = make_ingredient(user.business_id, canonical_unit="MILLILITRE")
    db = _fake_db_for(user, [oil])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=oil.id, quantity="5", unit="KG",
                                 unit_cost_cents=10000),
    ])
    with pytest.raises(AppError) as exc:
        await create_goods_receipt(body=body, user=user, db=db)
    assert exc.value.code == "INVALID_UNIT"
    assert db.commits == 0


# ── Omitted cost falls back to standing cost, explicitly ───────────────────

async def test_omitted_cost_falls_back_to_standing_cost_not_zero():
    """unit_cost_cents is legally omittable. Before the fix this recorded
    R0.00 on the receipt while the movement silently used the standing
    cost -- two disagreeing numbers. After the fix, both the line/header
    and the movement agree, using the ingredient's standing cost."""
    user = make_user()
    ingredient = make_ingredient(
        user.business_id, canonical_unit="GRAM", cost_micro_cents=44_000_000  # 44c/g = R440/kg
    )
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="500", unit="G"),
        # unit_cost_cents omitted entirely
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    # 500 g * 44c/g = 22000c = R220.00 -- not R0.00
    assert result["total_cost_cents"] == 22000
    line = lines_of(db)[0]
    assert line.unit_cost_cents is None  # what was actually entered, preserved
    assert line.line_cost_cents == 22000
    movement = movements_of(db)[0]
    assert movement.value_cents == 22000
    assert movement.unit_cost_micro_cents == 44_000_000


# ── Historical immutability ─────────────────────────────────────────────────

async def test_ingredient_cost_change_does_not_alter_existing_receipt():
    """A historical receipt must retain the cost recorded at the time.
    Changing the ingredient's standing cost afterwards must not alter the
    already-created receipt line or its ledger movement."""
    user = make_user()
    ingredient = make_ingredient(
        user.business_id, canonical_unit="GRAM", cost_micro_cents=50_000_000  # 50c/g
    )
    db = _fake_db_for(user, [ingredient])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=ingredient.id, quantity="1", unit="KG",
                                 unit_cost_cents=8000),  # R80/kg, this receipt's own price
    ])
    await create_goods_receipt(body=body, user=user, db=db)

    line = lines_of(db)[0]
    movement = movements_of(db)[0]
    assert line.line_cost_cents == 8000
    assert movement.value_cents == 8000
    assert movement.unit_cost_micro_cents == 8_000_000

    # The ingredient's standing cost changes later (e.g. a price increase
    # captured on the next delivery / a manager edit).
    ingredient.cost_per_canonical_micro_cents = 999_000_000  # unrelated new price

    # The already-recorded line and movement are plain, already-persisted
    # values -- nothing here re-derives from the ingredient's current cost.
    assert line.line_cost_cents == 8000
    assert movement.value_cents == 8000
    assert movement.unit_cost_micro_cents == 8_000_000


# ── Single-submit produces exactly one receipt / lines / movements ─────────

async def test_single_submit_creates_exactly_one_receipt_worth_of_rows():
    """No duplication within one request: N lines in -> N GoodsReceiptLine
    rows and N StockMovement rows out, never more. This is the scope of
    idempotency this fix can verify -- see KNOWN_BUGS.md BUG-0112 for the
    separate, pre-existing finding that the endpoint has no cross-request
    (double-click / retry) idempotency key at all, which this defect fix
    does not add (feature freeze; out of scope for a costing bug fix)."""
    user = make_user()
    a = make_ingredient(user.business_id, canonical_unit="GRAM")
    b = make_ingredient(user.business_id, canonical_unit="UNIT")
    db = _fake_db_for(user, [a, b])

    body = GoodsReceiptCreateRequest(lines=[
        GoodsReceiptLineRequest(ingredient_id=a.id, quantity="1", unit="KG",
                                 unit_cost_cents=1000),
        GoodsReceiptLineRequest(ingredient_id=b.id, quantity="3", unit="EACH",
                                 unit_cost_cents=500),
    ])
    result = await create_goods_receipt(body=body, user=user, db=db)

    assert result["lines"] == 2
    assert len(lines_of(db)) == 2
    assert len(movements_of(db)) == 2
    assert db.commits == 1
