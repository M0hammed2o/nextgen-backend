"""
Delivery module — pure-function tests (no DB, no network): the base.py
capability guard, the registry's webhook allow-list, the Mr D disabled
stub, and Uber's signature/webhook-envelope/order-details mapping.
"""

import hashlib
import hmac

import pytest

from backend.app.delivery.base import (
    DeliveryCapabilities,
    DeliveryOrderContext,
    DeliveryProviderAdapter,
)
from backend.app.delivery.canonical import CanonicalDeliveryOrder, DeliveryOrderStatus
from backend.app.delivery.errors import DeliveryCapabilityUnsupported, DeliveryProviderNotImplemented
from backend.app.delivery.mrd.adapter import MrDAdapter
from backend.app.delivery.registry import get_provider, is_webhook_enabled
from backend.app.delivery.uber import mapping as uber_mapping
from backend.app.delivery.uber.adapter import UberEatsAdapter


# ── base.py — the capability guard is enforced by the ABC itself ────────────

class TestCapabilityGuard:
    def test_subclass_cannot_bypass_the_guard_by_only_implementing_the_private_method(self):
        """
        A subclass implements `_verify_webhook_signature` etc, never the
        public method — so there is no code path where it could accidentally
        skip `_require()`.
        """
        class _NoWebhooksAdapter(DeliveryProviderAdapter):
            provider = "TEST"
            capabilities = DeliveryCapabilities()  # everything False

            def _verify_webhook_signature(self, raw_body, headers):
                return True  # would be wrong if ever reached

            def _parse_webhook_event(self, raw_body, headers):
                return None

            async def _fetch_order(self, *, external_order_id, context):
                raise AssertionError("unreachable")

            async def _accept_order(self, *, external_order_id, context, our_order_id):
                raise AssertionError("unreachable")

            async def _deny_order(self, *, external_order_id, context, reason_code, explanation):
                raise AssertionError("unreachable")

            async def _get_store_status(self, *, context):
                raise AssertionError("unreachable")

            async def _set_store_status(self, *, context, status, paused_until, reason):
                raise AssertionError("unreachable")

            async def _upsert_menu(self, *, context, menu_payload):
                raise AssertionError("unreachable")

            def map_order_status(self, canonical):
                return DeliveryOrderStatus.UNKNOWN

        adapter = _NoWebhooksAdapter()
        with pytest.raises(DeliveryCapabilityUnsupported):
            adapter.verify_webhook_signature(b"{}", {})
        with pytest.raises(DeliveryCapabilityUnsupported):
            adapter.parse_webhook_event(b"{}", {})

    async def test_accept_deny_requires_capability(self):
        adapter = MrDAdapter()
        context = DeliveryOrderContext(
            business_id=__import__("uuid").uuid4(), connection_id=__import__("uuid").uuid4(),
            external_store_id="store1",
        )
        with pytest.raises(DeliveryCapabilityUnsupported):
            await adapter.accept_order(external_order_id="o1", context=context, our_order_id="x")


# ── Mr D — disabled stub ─────────────────────────────────────────────────────

class TestMrDDisabledStub:
    def test_every_capability_flag_is_false(self):
        adapter = MrDAdapter()
        caps = adapter.capabilities
        assert caps.webhooks is False
        assert caps.accept_deny is False
        assert caps.menu_sync is False
        assert caps.store_pause_resume is False
        assert caps.item_availability is False
        assert caps.store_discovery is False
        assert caps.settlement_report is False

    def test_verify_webhook_signature_raises_capability_unsupported_before_reaching_the_stub_body(self):
        adapter = MrDAdapter()
        with pytest.raises(DeliveryCapabilityUnsupported):
            adapter.verify_webhook_signature(b"{}", {})

    def test_private_stub_body_returns_false_unconditionally_if_ever_called_directly(self):
        adapter = MrDAdapter()
        assert adapter._verify_webhook_signature(b"{}", {"x-mrd-signature": "anything"}) is False

    def test_network_methods_raise_not_implemented(self):
        adapter = MrDAdapter()
        with pytest.raises(DeliveryProviderNotImplemented):
            adapter._parse_webhook_event(b"{}", {})

    def test_map_order_status_is_always_unknown(self):
        adapter = MrDAdapter()
        canonical = CanonicalDeliveryOrder(
            provider="MR_D", external_order_id="1", external_store_id="s", provider_status_raw="ANYTHING",
        )
        assert adapter.map_order_status(canonical) == DeliveryOrderStatus.UNKNOWN


# ── registry.py — MR_D must never be webhook-enabled ─────────────────────────

class TestRegistry:
    def test_uber_eats_is_webhook_enabled(self):
        assert is_webhook_enabled("UBER_EATS") is True

    def test_mr_d_is_not_webhook_enabled(self):
        assert is_webhook_enabled("MR_D") is False

    def test_unknown_provider_is_not_webhook_enabled(self):
        assert is_webhook_enabled("DOORDASH") is False
        assert is_webhook_enabled(None) is False

    def test_get_provider_returns_correct_adapter_types(self):
        assert isinstance(get_provider("UBER_EATS"), UberEatsAdapter)
        assert isinstance(get_provider("MR_D"), MrDAdapter)
        assert get_provider("DOORDASH") is None

    def test_provider_lookup_is_case_insensitive(self):
        assert isinstance(get_provider("uber_eats"), UberEatsAdapter)


# ── Uber signature verification ──────────────────────────────────────────────

class TestUberSignature:
    def test_valid_signature_verifies(self):
        secret = "test-client-secret"
        body = b'{"event_id": "abc"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert uber_mapping.verify_signature(body, sig, secret) is True

    def test_tampered_body_fails_verification(self):
        secret = "test-client-secret"
        body = b'{"event_id": "abc"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        tampered = b'{"event_id": "abc-tampered"}'
        assert uber_mapping.verify_signature(tampered, sig, secret) is False

    def test_missing_signature_fails_closed(self):
        assert uber_mapping.verify_signature(b"{}", None, "secret") is False

    def test_missing_secret_fails_closed(self):
        sig = hmac.new(b"whatever", b"{}", hashlib.sha256).hexdigest()
        assert uber_mapping.verify_signature(b"{}", sig, "") is False

    def test_no_sha256_prefix_unlike_meta(self):
        """Uber's header is the bare hex digest — no 'sha256=' prefix."""
        secret = "s"
        body = b"{}"
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert not sig.startswith("sha256=")
        assert uber_mapping.verify_signature(body, sig, secret) is True


# ── Uber webhook envelope parsing ────────────────────────────────────────────

class TestUberWebhookEnvelope:
    def test_parses_confirmed_fields(self):
        payload = {
            "event_id": "evt-1",
            "event_time": 1700000000,
            "event_type": "orders.notification",
            "meta": {"user_id": "store-123", "resource_id": "order-456", "status": "created"},
            "resource_href": "https://api.uber.com/v1/something",
        }
        envelope = uber_mapping.parse_webhook_envelope(payload)
        assert envelope.event_id == "evt-1"
        assert envelope.external_store_id == "store-123"
        assert envelope.external_order_id == "order-456"
        assert envelope.event_type == "orders.notification"

    def test_missing_event_id_returns_none(self):
        assert uber_mapping.parse_webhook_envelope({"meta": {}}) is None

    def test_non_dict_payload_returns_none(self):
        assert uber_mapping.parse_webhook_envelope(["not", "a", "dict"]) is None

    def test_resource_href_is_never_read_into_the_envelope(self):
        """SSRF hardening — nothing in WebhookEnvelope carries resource_href at all."""
        payload = {"event_id": "e", "meta": {}, "resource_href": "http://evil.internal/steal"}
        envelope = uber_mapping.parse_webhook_envelope(payload)
        assert not hasattr(envelope, "resource_href")


# ── Uber current_state -> canonical status mapping ───────────────────────────

class TestMapCurrentState:
    @pytest.mark.parametrize("raw,expected", [
        ("CREATED", DeliveryOrderStatus.RECEIVED),
        ("ACCEPTED", DeliveryOrderStatus.ACCEPTED),
        ("DENIED", DeliveryOrderStatus.DENIED),
        ("FINISHED", DeliveryOrderStatus.FULFILLED),
        ("CANCELED", DeliveryOrderStatus.CANCELLED),
        ("UNKNOWN", DeliveryOrderStatus.UNKNOWN),
        ("something_new_uber_invented", DeliveryOrderStatus.UNKNOWN),
        (None, DeliveryOrderStatus.UNKNOWN),
    ])
    def test_mapping(self, raw, expected):
        assert uber_mapping.map_current_state(raw) == expected


# ── Get Order Details parsing ────────────────────────────────────────────────

class TestParseOrderDetails:
    def test_maps_confirmed_fields(self):
        raw = {
            "id": "order-1",
            "display_id": "A1B2",
            "current_state": "CREATED",
            "type": "DELIVERY_BY_UBER",
            "brand": "Ciao Pizza",
            "placed_at": "2026-09-22T10:00:00Z",
            "eater": {"first_name": "Sam", "phone": "5551234", "phone_code": "+1"},
            "cart": {
                "items": [
                    {
                        "id": "item-1",
                        "external_data": "menu-item-large-margherita",
                        "title": "Large Margherita",
                        "quantity": 2,
                        "price": {"amount": 15000},
                        "selected_modifier_groups": [
                            {"selected_items": [{"id": "mod-1", "title": "Extra Cheese", "quantity": 1, "price": {"amount": 1000}}]}
                        ],
                    }
                ],
                "fulfillment_issues": [],
            },
            "payment": {
                "charges": {
                    "sub_total": 30000, "tax": 4500, "total_fee": 3000, "total": 34500, "cash_amount_due": 0,
                }
            },
        }
        canonical = uber_mapping.parse_order_details(raw, external_store_id="store-abc")
        assert canonical.external_order_id == "order-1"
        assert canonical.external_store_id == "store-abc"  # from the caller, never from `raw`
        assert canonical.provider_status_raw == "CREATED"
        assert canonical.subtotal_cents == 30000
        assert canonical.tax_cents == 4500
        assert canonical.fee_cents == 3000
        assert canonical.total_cents == 34500
        assert canonical.restaurant_net_cents is None  # never inferred
        assert canonical.delivery_address is None
        assert len(canonical.lines) == 1
        line = canonical.lines[0]
        assert line.external_item_id == "menu-item-large-margherita"
        assert line.quantity == 2
        assert line.unit_price_cents == 15000
        assert len(line.modifiers) == 1
        assert line.modifiers[0].title == "Extra Cheese"
        assert line.modifiers[0].price_cents == 1000
        assert canonical.customer_first_name == "Sam"
        assert canonical.customer_phone == "+15551234"

    def test_missing_price_object_does_not_crash(self):
        raw = {"id": "o1", "cart": {"items": [{"title": "Mystery Item", "quantity": 1}]}}
        canonical = uber_mapping.parse_order_details(raw, external_store_id="s1")
        assert canonical.lines[0].unit_price_cents is None

    def test_bare_int_money_values_accepted(self):
        raw = {"id": "o1", "payment": {"charges": {"total": 1000}}}
        canonical = uber_mapping.parse_order_details(raw, external_store_id="s1")
        assert canonical.total_cents == 1000

    def test_provider_metadata_is_allow_listed_not_a_raw_dump(self):
        raw = {
            "id": "o1", "brand": "Ciao Pizza", "type": "DELIVERY_BY_UBER", "current_state": "CREATED",
            "eater": {"first_name": "Secret Name", "phone": "0000000"},
        }
        canonical = uber_mapping.parse_order_details(raw, external_store_id="s1")
        assert set(canonical.provider_metadata.keys()) <= {"brand", "type", "current_state"}
        assert "eater" not in canonical.provider_metadata
        assert "Secret Name" not in str(canonical.provider_metadata)
