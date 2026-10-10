"""
Delivery reconciliation entitlement (migration 0054) — admin-plane round
trip.

UI V2.1 follow-up: delivery_reconciliation_enabled is a DELIBERATELY
SEPARATE commercial entitlement from delivery_platforms_enabled — it gates
the CSV/manual delivery-order reconciliation importer
(routes_external_orders.py), not the live marketplace integrations. See
shared/models/business.py for the full rationale.

Follows the exact same pattern already proven for
loyalty_enabled/advanced_analytics_enabled/delivery_platforms_enabled
(BUG-0072 and its predecessors). Confirms:
  - a SUPER_ADMIN PATCH really sets the model attribute and the response
    reflects it (PATCH -> GET round trip);
  - an ONBOARDER is refused (403 FIELD_NOT_ALLOWED) — this flag is
    deliberately NOT in ONBOARDER_PATCH_ALLOWED_FIELDS, the same "platform
    decision, not a sales-partner toggle" rule as the other module flags;
  - it is returned distinctly from, and never derived from,
    delivery_platforms_enabled.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

from admin_api.app.api.v1.routes_businesses import (
    ONBOARDER_PATCH_ALLOWED_FIELDS,
    BusinessAdminResponse,
    BusinessOnboarderResponse,
    BusinessUpdate,
    _business_response,
    update_business,
)
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from shared.models.admin import AdminRole


class FakeDB:
    """Minimal async-session double: get_business_scoped uses db.get(); the
    PATCH handler uses db.add()/db.commit()/db.refresh()."""

    def __init__(self, business):
        self.business = business
        self.added = []
        self.commits = 0

    async def get(self, model, id_):
        return self.business

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass


def _admin_user(role=AdminRole.SUPER_ADMIN) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=None, role=role, token_type="admin_access")


def _make_business(**overrides):
    now = datetime.now(timezone.utc)
    business = types.SimpleNamespace(
        id=uuid.uuid4(), name="Test Biz", slug="test-biz", business_code="ABC123",
        is_active=True, suspended_reason=None, timezone="Africa/Johannesburg",
        plan="STARTER", billing_status="TRIAL", currency="ZAR",
        whatsapp_phone_number_id=None, stripe_customer_id=None, stripe_subscription_id=None,
        daily_message_limit=800, daily_llm_call_limit=400, daily_order_limit=200,
        last_webhook_received_at=None, created_at=now,
        is_whatsapp_enabled=True, inventory_enabled=False, digital_receipts_enabled=False,
        delivery_platforms_enabled=False, uber_eats_enabled=False, mrd_enabled=False,
        delivery_reconciliation_enabled=False,
        advanced_analytics_enabled=False,
        loyalty_enabled=False,
        staff_live_orders_tab_enabled=True, staff_wastage_tab_enabled=True,
        staff_stocktake_tab_enabled=False,
        onboarded_by_admin_id=None, onboarding_completed_at=None,
    )
    for k, v in overrides.items():
        setattr(business, k, v)
    return business


class TestDeliveryReconciliationEntitlementRoundTrip:
    async def test_super_admin_can_enable_delivery_reconciliation_and_response_reflects_it(self):
        business = _make_business(delivery_reconciliation_enabled=False)
        db = FakeDB(business)
        admin = _admin_user(AdminRole.SUPER_ADMIN)

        body = BusinessUpdate(delivery_reconciliation_enabled=True)
        result = await update_business(business_id=business.id, body=body, user=admin, db=db)

        assert isinstance(result, BusinessAdminResponse)
        assert result.delivery_reconciliation_enabled is True
        assert business.delivery_reconciliation_enabled is True  # PATCH really set it
        assert db.commits == 1

    async def test_super_admin_can_disable_delivery_reconciliation(self):
        business = _make_business(delivery_reconciliation_enabled=True)
        db = FakeDB(business)
        admin = _admin_user(AdminRole.SUPER_ADMIN)

        body = BusinessUpdate(delivery_reconciliation_enabled=False)
        result = await update_business(business_id=business.id, body=body, user=admin, db=db)

        assert result.delivery_reconciliation_enabled is False
        assert business.delivery_reconciliation_enabled is False

    async def test_setting_it_never_touches_delivery_platforms_enabled(self):
        """The two entitlements are independent -- flipping one must never
        move the other, in either direction."""
        business = _make_business(delivery_platforms_enabled=True, delivery_reconciliation_enabled=False)
        db = FakeDB(business)
        admin = _admin_user(AdminRole.SUPER_ADMIN)

        body = BusinessUpdate(delivery_reconciliation_enabled=True)
        result = await update_business(business_id=business.id, body=body, user=admin, db=db)

        assert result.delivery_reconciliation_enabled is True
        assert result.delivery_platforms_enabled is True  # untouched, still true
        assert business.delivery_platforms_enabled is True

    async def test_onboarder_cannot_set_delivery_reconciliation_enabled(self):
        business = _make_business(delivery_reconciliation_enabled=False)
        db = FakeDB(business)
        onboarder = _admin_user(AdminRole.ONBOARDER)
        # get_business_scoped requires the ONBOARDER to own this business and
        # onboarding to still be in progress -- satisfy both so the ONLY
        # reason the PATCH can fail is the field allow-list, not scoping.
        business.onboarded_by_admin_id = onboarder.user_id
        business.onboarding_completed_at = None

        body = BusinessUpdate(delivery_reconciliation_enabled=True)
        with pytest.raises(AppError) as exc:
            await update_business(business_id=business.id, body=body, user=onboarder, db=db)

        assert exc.value.code == "FIELD_NOT_ALLOWED"
        assert exc.value.status_code == 403
        assert "delivery_reconciliation_enabled" in exc.value.message
        assert db.commits == 0
        assert business.delivery_reconciliation_enabled is False  # untouched

    def test_onboarder_can_still_read_it_read_only(self):
        """Same rationale as delivery_platforms_enabled: an onboarder can't
        grant it, but can see current status to know whether to escalate."""
        business = _make_business(delivery_reconciliation_enabled=True)
        response = _business_response(business, AdminRole.ONBOARDER)

        assert isinstance(response, BusinessOnboarderResponse)
        assert response.delivery_reconciliation_enabled is True


def test_delivery_reconciliation_enabled_not_in_onboarder_allowed_fields():
    assert "delivery_reconciliation_enabled" not in ONBOARDER_PATCH_ALLOWED_FIELDS
