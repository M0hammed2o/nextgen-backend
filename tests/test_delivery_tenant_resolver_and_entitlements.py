"""
`tenant_resolver.py` (the trust boundary) and `entitlements.py`.

Cross-tenant scenario: two businesses each have their own
`delivery_connections` row for the SAME provider with DIFFERENT
`external_store_id` values. A webhook reporting business A's store id must
resolve ONLY to business A's connection, never business B's — enforced here
at the query level (a real Postgres partial unique index backs this too,
see migration 0040, not re-verified against a live DB in this session).
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from backend.app.delivery.entitlements import is_connection_live, is_provider_entitled
from backend.app.delivery.tenant_resolver import resolve_delivery_connection
from shared.models.business import Business
from shared.models.delivery import DeliveryConnection


class ScalarResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeDB:
    """Returns connections filtered by (provider, external_store_id) like a real query would."""

    def __init__(self, connections: list[DeliveryConnection]):
        self._connections = connections

    async def execute(self, stmt):
        # Extract the bound WHERE values SQLAlchemy compiled in, without a
        # live connection — inspect the compiled parameters directly.
        compiled = stmt.compile()
        params = compiled.params
        provider = params.get("provider_1")
        store_id = params.get("external_store_id_1")
        match = next(
            (c for c in self._connections if c.provider == provider and c.external_store_id == store_id),
            None,
        )
        return ScalarResult(scalar=match)


class TestCrossTenantResolution:
    async def test_two_businesses_same_provider_different_store_ids_never_cross_resolve(self):
        business_a = uuid.uuid4()
        business_b = uuid.uuid4()
        conn_a = DeliveryConnection(
            id=uuid.uuid4(), business_id=business_a, provider="UBER_EATS",
            external_store_id="store-A", status="ACTIVE", environment="production",
        )
        conn_b = DeliveryConnection(
            id=uuid.uuid4(), business_id=business_b, provider="UBER_EATS",
            external_store_id="store-B", status="ACTIVE", environment="production",
        )
        db = FakeDB([conn_a, conn_b])

        resolved_a = await resolve_delivery_connection(db, provider="UBER_EATS", external_store_id="store-A")
        resolved_b = await resolve_delivery_connection(db, provider="UBER_EATS", external_store_id="store-B")

        assert resolved_a.business_id == business_a
        assert resolved_b.business_id == business_b
        assert resolved_a.business_id != resolved_b.business_id

    async def test_a_store_id_claimed_by_no_connection_resolves_to_none_not_a_guess(self):
        db = FakeDB([])
        resolved = await resolve_delivery_connection(db, provider="UBER_EATS", external_store_id="store-nobody-owns")
        assert resolved is None

    async def test_missing_store_id_never_queries_and_returns_none(self):
        db = AsyncMock()
        resolved = await resolve_delivery_connection(db, provider="UBER_EATS", external_store_id=None)
        assert resolved is None
        db.execute.assert_not_called()

    async def test_missing_provider_never_queries_and_returns_none(self):
        db = AsyncMock()
        resolved = await resolve_delivery_connection(db, provider="", external_store_id="store-A")
        assert resolved is None
        db.execute.assert_not_called()


class TestEntitlements:
    def _business(self, **overrides) -> Business:
        b = Business(
            id=uuid.uuid4(), name="T", slug="t", business_code="ABC123",
            delivery_platforms_enabled=True, uber_eats_enabled=True, mrd_enabled=False,
        )
        for k, v in overrides.items():
            setattr(b, k, v)
        return b

    def test_both_flags_must_be_true(self):
        assert is_provider_entitled(self._business(), "UBER_EATS") is True

    def test_module_switch_off_denies_even_if_provider_flag_is_on(self):
        b = self._business(delivery_platforms_enabled=False, uber_eats_enabled=True)
        assert is_provider_entitled(b, "UBER_EATS") is False

    def test_provider_flag_off_denies_even_if_module_switch_is_on(self):
        b = self._business(delivery_platforms_enabled=True, uber_eats_enabled=False)
        assert is_provider_entitled(b, "UBER_EATS") is False

    def test_mrd_uses_its_own_flag_not_uber_eats(self):
        b = self._business(delivery_platforms_enabled=True, uber_eats_enabled=True, mrd_enabled=False)
        assert is_provider_entitled(b, "MR_D") is False

    def test_none_business_is_never_entitled(self):
        assert is_provider_entitled(None, "UBER_EATS") is False

    def test_unknown_provider_is_never_entitled(self):
        assert is_provider_entitled(self._business(), "DOORDASH") is False

    def test_delivery_platforms_enabled_is_distinct_from_the_whatsapp_own_driver_flag(self):
        """
        Business.delivery_enabled (WhatsApp own-driver delivery) must never
        be read by is_provider_entitled — they are deliberately different
        columns (see shared/models/business.py).
        """
        b = self._business(delivery_platforms_enabled=False)
        b.delivery_enabled = True  # the OTHER, unrelated flag, turned on
        assert is_provider_entitled(b, "UBER_EATS") is False

    def test_connection_liveness(self):
        active = DeliveryConnection(status="ACTIVE")
        paused = DeliveryConnection(status="PAUSED")
        assert is_connection_live(active) is True
        assert is_connection_live(paused) is False
        assert is_connection_live(None) is False
