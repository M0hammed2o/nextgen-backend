"""
POS pricing resolution — backend/app/pos/pricing.py.

Confirms the gap this module exists to close: every menu item / option /
add-on ID a POS client submits is re-validated against this business's own
live catalog before any price is trusted, and the total is always computed
via shared/pricing/engine.py, never from a client-submitted price.
"""
import types
import uuid

import pytest

from backend.app.core.errors import AppError, NotFoundError
from backend.app.pos.pricing import POSAddOnSelection, POSItemInput, resolve_pos_items


# ── Fakes ────────────────────────────────────────────────────────────────────

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
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0)


def make_menu_item(business_id, price_cents=5000, options_json=None, add_ons=None,
                    is_active=True, is_deleted=False):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=business_id,
        name="Iced Coffee",
        price_cents=price_cents,
        options_json=options_json,
        is_active=is_active,
        is_deleted=is_deleted,
        add_ons=add_ons or [],
    )


def make_add_on(business_id, price_cents=1000, min_qty=0, max_qty=5,
                is_active=True, is_deleted=False, name="Extra Shot"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        business_id=business_id,
        name=name,
        price_cents=price_cents,
        min_qty=min_qty,
        max_qty=max_qty,
        is_active=is_active,
        is_deleted=is_deleted,
    )


SIZE_GROUP_ID = "size-group"


def size_options_json(mkhulu_delta=1500, medium_delta=-1000):
    return {
        "option_groups": [
            {
                "id": SIZE_GROUP_ID,
                "name": "Size",
                "required": True,
                "is_enabled": True,
                "options": [
                    {"id": "medium", "name": "Medium", "price_delta_cents": medium_delta, "is_enabled": True},
                    {"id": "regular", "name": "Regular", "price_delta_cents": 0, "is_enabled": True},
                    {"id": "mkhulu", "name": "MKhulu", "price_delta_cents": mkhulu_delta, "is_enabled": True},
                ],
            }
        ]
    }


# ── Tenant isolation ─────────────────────────────────────────────────────────

async def test_menu_item_from_another_business_is_404():
    business_id = uuid.uuid4()
    other_business_item = make_menu_item(uuid.uuid4())  # belongs to a different business
    db = FakeDB([FakeResult([])])  # query filters by business_id, so nothing matches

    with pytest.raises(NotFoundError):
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(menu_item_id=other_business_item.id, quantity=1)],
        )


# ── Required option enforcement ──────────────────────────────────────────────

async def test_required_option_group_must_be_selected():
    business_id = uuid.uuid4()
    item = make_menu_item(business_id, options_json=size_options_json())
    db = FakeDB([FakeResult([item])])

    with pytest.raises(AppError) as exc:
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(menu_item_id=item.id, quantity=1, selected_option_ids={})],
        )
    assert exc.value.code == "MISSING_REQUIRED_OPTION"


async def test_invalid_option_id_is_rejected():
    business_id = uuid.uuid4()
    item = make_menu_item(business_id, options_json=size_options_json())
    db = FakeDB([FakeResult([item])])

    with pytest.raises(AppError) as exc:
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(menu_item_id=item.id, quantity=1,
                          selected_option_ids={SIZE_GROUP_ID: "does-not-exist"})],
        )
    assert exc.value.code == "INVALID_OPTION"


# ── Correct server-side pricing (the core gap this module closes) ──────────

async def test_price_computed_from_live_catalog_not_client_input():
    business_id = uuid.uuid4()
    add_on = make_add_on(business_id, price_cents=1000, max_qty=5)
    item = make_menu_item(
        business_id, price_cents=5000,
        options_json=size_options_json(mkhulu_delta=1500),
        add_ons=[add_on],
    )
    db = FakeDB([
        FakeResult([item]),      # MenuItem lookup
        FakeResult([add_on]),    # MenuAddOn lookup
    ])

    resolved = await resolve_pos_items(
        db, business_id,
        [POSItemInput(
            menu_item_id=item.id, quantity=2,
            selected_option_ids={SIZE_GROUP_ID: "mkhulu"},
            add_on_selections=[POSAddOnSelection(add_on_id=add_on.id, quantity=2)],
        )],
    )

    assert len(resolved) == 1
    breakdown = resolved[0].breakdown
    # base 5000 + option +1500 + add_on (1000 * 2 = 2000) = 8500 per unit
    assert breakdown.unit_price_cents == 8500
    assert breakdown.line_total_cents == 17000  # x2 quantity
    assert resolved[0].selected_options_snapshot[0]["option_name"] == "MKhulu"
    assert resolved[0].add_ons_snapshot[0]["quantity"] == 2


async def test_add_on_not_offered_on_item_is_rejected():
    business_id = uuid.uuid4()
    unrelated_add_on = make_add_on(business_id)
    item = make_menu_item(business_id, add_ons=[])  # no add-ons attached
    db = FakeDB([FakeResult([item])])

    with pytest.raises(AppError) as exc:
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(
                menu_item_id=item.id, quantity=1,
                add_on_selections=[POSAddOnSelection(add_on_id=unrelated_add_on.id, quantity=1)],
            )],
        )
    assert exc.value.code == "INVALID_ADD_ON"


async def test_add_on_quantity_above_max_is_rejected():
    business_id = uuid.uuid4()
    add_on = make_add_on(business_id, max_qty=3)
    item = make_menu_item(business_id, add_ons=[add_on])
    db = FakeDB([
        FakeResult([item]),
        FakeResult([add_on]),
    ])

    with pytest.raises(AppError) as exc:
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(
                menu_item_id=item.id, quantity=1,
                add_on_selections=[POSAddOnSelection(add_on_id=add_on.id, quantity=10)],
            )],
        )
    assert exc.value.code == "INVALID_ADD_ON_QUANTITY"


async def test_inactive_menu_item_is_rejected():
    business_id = uuid.uuid4()
    item = make_menu_item(business_id, is_active=False)
    db = FakeDB([FakeResult([item])])

    with pytest.raises(AppError) as exc:
        await resolve_pos_items(
            db, business_id,
            [POSItemInput(menu_item_id=item.id, quantity=1)],
        )
    assert exc.value.code == "MENU_ITEM_INACTIVE"
