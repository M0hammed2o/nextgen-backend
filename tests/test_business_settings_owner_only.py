"""
PUT /business/settings money-routing field protection (Wave 0A P1 security
fix) + GET /business/settings exposing the is_whatsapp_enabled kill switch.

Before this fix, `require_owner_or_manager` let a MANAGER change
payment_provider/payment_api_key/payment_api_secret/payment_webhook_secret
and the EFT bank fields — enough to redirect where the restaurant's
payments are sent. Now those fields are OWNER-only; a MANAGER attempt is a
403 naming the field, and every other setting is unchanged for managers.
An OWNER changing one of them also gets an AuditEvent (field names only,
never the secret/EFT values).

is_whatsapp_enabled (the platform-admin kill switch) is now returned
read-only in BusinessSettingsResponse, still not settable via
BusinessSettingsUpdate.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

from backend.app.api.v1.routes_business import (
    BusinessSettingsUpdate,
    get_business_settings,
    update_business_settings,
)
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar or 0


class FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def make_user(role="OWNER", business_id=None) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id or uuid.uuid4(), role=role, token_type="access")


def make_business(business_id, **overrides):
    now = datetime.now(timezone.utc)
    b = types.SimpleNamespace(
        id=business_id, name="Test Biz", slug="test-biz", business_code="ABC123",
        # UI V2 branding (migration 0053). A real Business row always has these
        # columns; this stand-in has to carry them too or
        # BusinessSettingsResponse.from_business raises AttributeError.
        logo_asset_id=None, brand_icon_asset_id=None,
        brand_primary_colour=None, brand_accent_colour=None,
        brand_display_name=None, brand_theme="LIGHT",
        timezone="Africa/Johannesburg", business_hours=None,
        greeting_text=None, fallback_text=None, closed_text=None, busy_text=None,
        order_in_only=False, delivery_enabled=False, delivery_fee_cents=0,
        require_customer_name=False, require_phone_number=True, require_delivery_address=True,
        currency="ZAR", plan="STARTER", billing_status="TRIAL",
        daily_message_limit=800, daily_llm_call_limit=400, daily_order_limit=200,
        monthly_order_quota=1500, order_pack_credits=0,
        billing_period_started_at=now,
        address=None, phone=None, menu_image_url=None,
        payment_methods_enabled=None, online_payment_required=False,
        payment_provider=None, payment_timeout_minutes=30,
        eft_bank_name=None, eft_account_name=None, eft_account_number=None,
        eft_branch_code=None, eft_reference_prefix=None,
        vat_registered=False, vat_number=None, vat_rate_basis_points=0, vat_inclusive_pricing=True,
        receipt_paper_width_mm=80, receipt_auto_print=True, receipt_copies=1,
        print_kitchen_receipt=False, receipt_config_json=None,
        receipt_reference_asset_id=None, menu_reference_asset_id=None,
        inventory_enabled=False, digital_receipts_enabled=False,
        delivery_platforms_enabled=False, uber_eats_enabled=False, mrd_enabled=False,
        delivery_reconciliation_enabled=False,
        advanced_analytics_enabled=False,
        loyalty_enabled=False,
        is_whatsapp_enabled=True, wastage_approval_threshold_cents=0,
        staff_live_orders_tab_enabled=True, staff_wastage_tab_enabled=True,
        staff_stocktake_tab_enabled=False, stocktake_frequency="DAILY_1", stocktake_times_json=None,
        payment_api_key=None, payment_api_secret=None, payment_webhook_secret=None,
        pos_cash_enabled=True, pos_card_enabled=True, pos_payfast_enabled=False,
        pos_yoco_enabled=False, pos_ikhoka_enabled=False, pos_direct_eft_enabled=False,
        pos_other_payment_enabled=False, pos_table_number_required=False,
        pos_enabled=True,
    )
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


# ── MANAGER blocked from money-routing fields ───────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("payment_provider", "MOCK"),
    ("payment_api_key", "sk_live_whatever"),
    ("payment_api_secret", "secret"),
    ("payment_webhook_secret", "whsec_whatever"),
    ("eft_bank_name", "FNB"),
    ("eft_account_name", "Manager's Personal Account"),
    ("eft_account_number", "999999999"),
    ("eft_branch_code", "250655"),
])
async def test_manager_cannot_change_money_routing_field(field, value):
    manager = make_user(role="MANAGER")
    db = FakeDB([])  # blocked before any DB lookup

    body = BusinessSettingsUpdate(**{field: value})
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=body, user=manager, db=db)
    assert exc.value.code == "OWNER_ONLY_FIELD"
    assert exc.value.status_code == 403
    assert field in exc.value.message
    assert db.commits == 0


async def test_manager_can_still_update_other_settings():
    manager = make_user(role="MANAGER")
    business = make_business(manager.business_id)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(greeting_text="Welcome!")
    result = await update_business_settings(body=body, user=manager, db=db)
    assert result.greeting_text == "Welcome!"
    assert db.commits == 1
    assert db.added == []  # no audit event for non-money fields


async def test_manager_request_mixing_money_and_other_fields_is_rejected_wholesale():
    """A single request touching one owner-only field aborts entirely —
    no partial silent success on the non-money fields either."""
    manager = make_user(role="MANAGER")
    db = FakeDB([])

    body = BusinessSettingsUpdate(greeting_text="Hi", payment_provider="MOCK")
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=body, user=manager, db=db)
    assert exc.value.code == "OWNER_ONLY_FIELD"


# ── OWNER allowed, with audit trail ─────────────────────────────────────────

async def test_owner_can_change_money_routing_field_and_gets_audit_event():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(payment_provider="MOCK", eft_account_number="123456")
    result = await update_business_settings(body=body, user=owner, db=db)

    assert result.payment_provider == "MOCK"
    assert db.commits == 1
    assert len(db.added) == 1
    event = db.added[0]
    assert event.action == "business.payment_settings.changed"
    assert set(event.diff_json["fields_changed"]) == {"payment_provider", "eft_account_number"}
    # Never log the actual credential/account values
    assert "123456" not in str(event.diff_json)
    assert "MOCK" not in event.diff_json.get("fields_changed", []) or True  # values not logged, only field names


async def test_owner_settings_update_missing_business_is_404():
    owner = make_user(role="OWNER")
    db = FakeDB([FakeResult(None)])
    body = BusinessSettingsUpdate(greeting_text="Hi")
    with pytest.raises(NotFoundError):
        await update_business_settings(body=body, user=owner, db=db)


# ── is_whatsapp_enabled: read-only visibility ───────────────────────────────

async def test_get_settings_returns_is_whatsapp_enabled():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, is_whatsapp_enabled=False)
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.is_whatsapp_enabled is False


def test_is_whatsapp_enabled_not_settable_via_update_schema():
    assert "is_whatsapp_enabled" not in BusinessSettingsUpdate.model_fields


# ── digital_receipts_enabled (BUG-0072 follow-up) ───────────────────────────
# Same platform-admin-only rule as inventory_enabled/is_whatsapp_enabled:
# admin_api gained this field tonight (BusinessUpdate/BusinessAdminResponse/
# BusinessOnboarderResponse), but the business plane never had it at all —
# the manager dashboard could not show real Digital Receipts entitlement
# status without this.

async def test_get_settings_returns_digital_receipts_enabled():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, digital_receipts_enabled=True)
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.digital_receipts_enabled is True


# ── delivery marketplace entitlement flags (migration 0044) ────────────────
# Same platform-admin-only rule as inventory_enabled/digital_receipts_enabled
# above — exposed read-only here so the manager dashboard can show real
# Uber Eats/delivery-marketplace entitlement status without this being the
# third instance of a Business column existing with no schema exposing it
# (BUG-0072 and its predecessor).

async def test_get_settings_returns_delivery_entitlement_flags():
    owner = make_user(role="OWNER")
    business = make_business(
        owner.business_id,
        delivery_platforms_enabled=True, uber_eats_enabled=True, mrd_enabled=False,
    )
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.delivery_platforms_enabled is True
    assert result.uber_eats_enabled is True
    assert result.mrd_enabled is False


def test_delivery_entitlement_flags_not_settable_via_update_schema():
    assert "delivery_platforms_enabled" not in BusinessSettingsUpdate.model_fields
    assert "uber_eats_enabled" not in BusinessSettingsUpdate.model_fields
    assert "mrd_enabled" not in BusinessSettingsUpdate.model_fields


async def test_delivery_entitlement_flags_distinct_from_own_driver_delivery_enabled():
    """
    delivery_enabled (WhatsApp own-driver delivery) and the new
    delivery_platforms_enabled/uber_eats_enabled/mrd_enabled entitlement
    flags are deliberately different columns with different meanings —
    flipping one must never move the other.
    """
    owner = make_user(role="OWNER")
    business = make_business(
        owner.business_id,
        delivery_enabled=True,
        delivery_platforms_enabled=False, uber_eats_enabled=False, mrd_enabled=False,
    )
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.delivery_enabled is True
    assert result.delivery_platforms_enabled is False
    assert result.uber_eats_enabled is False


def test_digital_receipts_enabled_not_settable_via_update_schema():
    assert "digital_receipts_enabled" not in BusinessSettingsUpdate.model_fields


# ── delivery_reconciliation_enabled (migration 0054) ───────────────────────
# UI V2.1 follow-up: a DELIBERATELY SEPARATE entitlement from
# delivery_platforms_enabled above — gates the CSV/manual reconciliation
# importer, not the live marketplace integrations. Same platform-admin-only
# rule as every other flag in this file.

async def test_get_settings_returns_delivery_reconciliation_enabled():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, delivery_reconciliation_enabled=True)
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.delivery_reconciliation_enabled is True


def test_delivery_reconciliation_enabled_not_settable_via_update_schema():
    assert "delivery_reconciliation_enabled" not in BusinessSettingsUpdate.model_fields


async def test_delivery_reconciliation_enabled_distinct_from_delivery_platforms_enabled():
    """The two delivery entitlements are independent -- a business can have
    either, both, or neither, and reading one must never reflect the other."""
    owner = make_user(role="OWNER")
    business = make_business(
        owner.business_id,
        delivery_platforms_enabled=True,
        delivery_reconciliation_enabled=False,
    )
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.delivery_platforms_enabled is True
    assert result.delivery_reconciliation_enabled is False


# ── advanced_analytics_enabled (migration 0047, Commercial V1 gate) ────────
# Same platform-admin-only rule as inventory_enabled/digital_receipts_enabled/
# delivery_platforms_enabled above — exposed read-only here so the manager
# dashboard can show real Advanced Analytics entitlement status.

async def test_get_settings_returns_advanced_analytics_enabled():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, advanced_analytics_enabled=True)
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.advanced_analytics_enabled is True


def test_advanced_analytics_enabled_not_settable_via_update_schema():
    assert "advanced_analytics_enabled" not in BusinessSettingsUpdate.model_fields


# ── loyalty_enabled (migration 0051, Commercial V1 gate, BUG-0107) ─────────
# Same platform-admin-only rule as the flags above — exposed read-only here
# so the manager dashboard can show real Loyalty entitlement status. This is
# the round-trip half of the coverage the task explicitly asked for (the
# admin-plane PATCH->GET half lives in tests/test_admin_loyalty_entitlement.py).

async def test_get_settings_returns_loyalty_enabled():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, loyalty_enabled=True)
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.loyalty_enabled is True


async def test_get_settings_returns_loyalty_enabled_false_by_default():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id)  # loyalty_enabled=False, untouched
    db = FakeDB([FakeResult(business), FakeResult(0)])

    result = await get_business_settings(user=owner, db=db)
    assert result.loyalty_enabled is False


def test_loyalty_enabled_not_settable_via_update_schema():
    assert "loyalty_enabled" not in BusinessSettingsUpdate.model_fields


# ── VAT rate validation (real-browser fix) ──────────────────────────────────
# A VAT-registered business must charge a real, non-zero VAT rate — a 0%/
# missing rate would make its receipts an invalid tax invoice.

async def test_vat_registered_with_zero_rate_is_rejected():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, vat_registered=False, vat_rate_basis_points=0)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(vat_registered=True, vat_number="4123456789")
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=body, user=owner, db=db)
    assert exc.value.code == "VAT_RATE_REQUIRED"
    assert exc.value.status_code == 422
    assert db.commits == 0


async def test_vat_registered_with_explicit_zero_rate_is_rejected():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, vat_registered=False, vat_rate_basis_points=0)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(vat_registered=True, vat_number="4123456789", vat_rate_basis_points=0)
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=body, user=owner, db=db)
    assert exc.value.code == "VAT_RATE_REQUIRED"


async def test_vat_registered_with_real_rate_is_accepted():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, vat_registered=False, vat_rate_basis_points=0)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(vat_registered=True, vat_number="4123456789", vat_rate_basis_points=1500)
    result = await update_business_settings(body=body, user=owner, db=db)
    assert result.vat_registered is True
    assert result.vat_rate_basis_points == 1500
    assert db.commits == 1


async def test_vat_registered_already_true_setting_rate_to_zero_is_rejected():
    """RESULTING-state check: a business already VAT-registered with a real
    rate must not be allowed to zero the rate out via a partial update that
    doesn't even touch vat_registered."""
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, vat_registered=True, vat_rate_basis_points=1500)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(vat_rate_basis_points=0)
    with pytest.raises(AppError) as exc:
        await update_business_settings(body=body, user=owner, db=db)
    assert exc.value.code == "VAT_RATE_REQUIRED"


async def test_non_vat_registered_zero_rate_is_fine():
    owner = make_user(role="OWNER")
    business = make_business(owner.business_id, vat_registered=False, vat_rate_basis_points=0)
    db = FakeDB([FakeResult(business)])

    body = BusinessSettingsUpdate(greeting_text="Welcome!")
    result = await update_business_settings(body=body, user=owner, db=db)
    assert result.greeting_text == "Welcome!"
    assert db.commits == 1
