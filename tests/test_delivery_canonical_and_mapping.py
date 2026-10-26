"""
Delivery module — pure-function tests (no DB, no network): the base.py
capability guard, the registry's webhook allow-list, the Mr D disabled
stub, and Uber's signature/webhook-envelope/order-details mapping.
"""

import hashlib
import hmac

import pytest
from unittest.mock import patch

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
    """
    Fixture shapes follow Uber's own v2 Get Order Details example
    (developer.uber.com/docs/eats/references/api/v2/get-eats-order-orderid,
    read 2026-10-26) — not guesses. The previous fixture used a flat
    `price.amount` and a "+1" `phone_code`, neither of which exists in that
    reference, so the old test passed while real payloads would have priced
    every line at R0.
    """

    @staticmethod
    def _money(amount, currency="ZAR"):
        return {"amount": amount, "currency_code": currency, "formatted_amount": f"R{amount / 100:.2f}"}

    def _documented_order(self):
        m = self._money
        return {
            "id": "f9f363d1-e1c2-4595-b477-c649845bc953",
            "display_id": "BC953",
            "current_state": "CREATED",
            "type": "DELIVERY_BY_UBER",
            "brand": "UBER_EATS",
            "placed_at": "2026-10-26T18:30:00+02:00",
            "estimated_ready_for_pickup_at": "2026-10-26T18:50:00+02:00",
            "store": {"id": "store-from-payload-NEVER-trusted"},
            "eater": {"first_name": "Larry", "phone": "+27 21 555 0100", "phone_code": "555 55 555"},
            "cart": {
                "special_instructions": "Ring the bell",
                "items": [
                    {
                        "id": "Muffin",
                        "instance_id": "Muffin-Instance",
                        "title": "Fresh-baked muffin",
                        "external_data": "free text set by some other integrator",
                        "quantity": 2,
                        "price": {
                            "unit_price": m(350), "total_price": m(700),
                            "base_unit_price": m(300), "base_total_price": m(600),
                        },
                        "selected_modifier_groups": [
                            {
                                "id": "Choose-flavor", "title": "Choose flavor",
                                "selected_items": [{
                                    "id": "Chocolate-deluxe", "title": "Chocolate deluxe", "quantity": 1,
                                    "price": {"unit_price": m(50), "total_price": m(50)},
                                }],
                                "removed_items": None,
                            },
                            {
                                "id": "Add-sugar", "title": "Add sugar", "selected_items": None,
                                "removed_items": [{
                                    "id": "Sugar", "title": "Sugar", "quantity": 0,
                                    "price": {"unit_price": m(0)}, "default_quantity": 2,
                                }],
                            },
                        ],
                    }
                ],
                "fulfillment_issues": [{
                    "fulfillment_issue_type": "OUT_OF_ITEM",
                    "fulfillment_action_type": "REMOVE_ITEM",
                    "root_item": {"id": "Donut", "title": "Chocolate donut"},
                }],
            },
            "payment": {
                "charges": {
                    "total": m(1399), "sub_total": m(700), "tax": m(0),
                    "total_fee": m(699), "cash_amount_due": m(0),
                }
            },
        }

    def test_maps_documented_fields(self):
        canonical = uber_mapping.parse_order_details(self._documented_order(), external_store_id="store-abc")
        assert canonical.external_order_id == "f9f363d1-e1c2-4595-b477-c649845bc953"
        assert canonical.display_id == "BC953"
        assert canonical.external_store_id == "store-abc"  # from the caller, never from `raw`
        assert canonical.provider_status_raw == "CREATED"
        assert canonical.order_type == "DELIVERY_BY_UBER"
        assert canonical.currency == "ZAR"
        assert canonical.subtotal_cents == 700
        assert canonical.tax_cents == 0
        assert canonical.fee_cents == 699
        assert canonical.total_cents == 1399
        assert canonical.restaurant_net_cents is None  # never inferred
        assert canonical.delivery_address is None
        assert canonical.special_instructions == "Ring the bell"
        assert canonical.ready_for_pickup_at is not None
        assert canonical.fulfillment_issues == ("OUT_OF_ITEM Chocolate donut (REMOVE_ITEM)",)

    def test_line_prices_come_from_the_nested_money_objects(self):
        line = uber_mapping.parse_order_details(self._documented_order(), external_store_id="s").lines[0]
        assert line.quantity == 2
        assert line.unit_price_cents == 350, "price.unit_price.amount — includes the selected modifier"
        assert line.total_price_cents == 700
        assert line.base_unit_price_cents == 300

    def test_mapping_key_is_the_entity_id_not_free_text_external_data(self):
        line = uber_mapping.parse_order_details(self._documented_order(), external_store_id="s").lines[0]
        assert line.external_item_id == "Muffin"

    def test_selected_and_removed_modifiers_are_both_captured(self):
        line = uber_mapping.parse_order_details(self._documented_order(), external_store_id="s").lines[0]
        added = [m for m in line.modifiers if not m.removed]
        removed = [m for m in line.modifiers if m.removed]
        assert [(m.external_id, m.title, m.price_cents, m.group_title) for m in added] == [
            ("Chocolate-deluxe", "Chocolate deluxe", 50, "Choose flavor"),
        ]
        assert [(m.external_id, m.title) for m in removed] == [("Sugar", "Sugar")]
        assert removed[0].price_cents is None, "taking an ingredient out never adds a charge"

    def test_phone_code_is_an_access_code_not_a_dialling_prefix(self):
        canonical = uber_mapping.parse_order_details(self._documented_order(), external_store_id="s")
        assert canonical.customer_first_name == "Larry"
        assert canonical.customer_phone == "+27 21 555 0100"
        assert canonical.customer_phone_code == "555 55 555"

    def test_flat_amount_price_is_still_accepted_as_a_fallback(self):
        raw = {"id": "o1", "cart": {"items": [{"id": "i", "title": "X", "quantity": 1, "price": {"amount": 1500}}]}}
        line = uber_mapping.parse_order_details(raw, external_store_id="s1").lines[0]
        assert line.unit_price_cents == 1500

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


class TestNonOrderEventsNeverBecomeOrders:
    def test_store_event_with_top_level_store_id_parses_without_an_order_id(self):
        envelope = uber_mapping.parse_webhook_envelope({
            "event_id": "evt-store-1", "event_type": "store.provisioned", "store_id": "store-9",
        })
        assert envelope.external_store_id == "store-9"
        assert envelope.external_order_id is None

    def test_unknown_event_type_carrying_a_resource_id_is_not_treated_as_an_order(self):
        """Uber: "new event types may be added in the future without notice"."""
        envelope = uber_mapping.parse_webhook_envelope({
            "event_id": "evt-x", "event_type": "delivery.state_changed",
            "meta": {"user_id": "store-1", "resource_id": "something-else"},
        })
        assert envelope.external_order_id is None

    @pytest.mark.parametrize("event_type", [
        "orders.notification", "orders.scheduled.notification",
        "orders.cancel", "orders.failure", "orders.customer_order_edit",
    ])
    def test_order_lifecycle_events_keep_their_order_id(self, event_type):
        envelope = uber_mapping.parse_webhook_envelope({
            "event_id": "e", "event_type": event_type,
            "meta": {"user_id": "store-1", "resource_id": "order-1"},
        })
        assert envelope.external_order_id == "order-1"


class TestMenuCatalogueParsing:
    def test_items_and_modifiers_are_classified_and_priced(self):
        raw = {
            "items": [
                {"id": "Coffee", "title": {"translations": {"en_us": "Coffee"}}, "price_info": {"price": 3000}},
                {"id": "Milk", "title": {"translations": {"en_us": "Milk"}}, "price_info": {"price": 500}},
                {"id": "Orphan", "title": {"translations": {"en_us": "Not on any menu"}}},
            ],
            "categories": [{"id": "Drinks", "entities": [{"type": "ITEM", "id": "Coffee"}]}],
            "modifier_groups": [
                {"id": "Add-milk", "title": {"translations": {"en_us": "Add milk"}},
                 "modifier_options": [{"type": "ITEM", "id": "Milk"}]},
                {"id": "Extras", "title": {"translations": {"en_us": "Extras"}},
                 "modifier_options": [{"type": "ITEM", "id": "Milk"}]},
            ],
        }
        entries = uber_mapping.parse_menu_catalogue(raw)
        by_key = {(e.kind, e.external_id): e for e in entries}
        assert set(by_key) == {("ITEM", "Coffee"), ("MODIFIER", "Milk")}, "one entry per entity per kind"
        assert by_key[("ITEM", "Coffee")].price_cents == 3000
        assert by_key[("MODIFIER", "Milk")].group_title == "Add milk"
        assert by_key[("MODIFIER", "Milk")].title == "Milk"

    def test_malformed_menu_degrades_to_nothing_not_a_crash(self):
        assert uber_mapping.parse_menu_catalogue({"items": "nope", "categories": [None]}) == []


class TestWebhookEnvironmentIsProvenBySignature:
    def _settings(self, **overrides):
        from backend.app.core.config import get_settings
        settings = get_settings()
        values = {
            "UBER_EATS_CLIENT_SECRET": "prod-secret", "UBER_EATS_SANDBOX_CLIENT_SECRET": "sandbox-secret",
        }
        values.update(overrides)
        return patch.multiple(settings, **values)

    def test_each_app_secret_proves_its_own_environment(self):
        from backend.app.delivery.uber.environments import match_webhook_environment

        body = b'{"event_id": "e"}'
        with self._settings():
            prod_sig = hmac.new(b"prod-secret", body, hashlib.sha256).hexdigest()
            sandbox_sig = hmac.new(b"sandbox-secret", body, hashlib.sha256).hexdigest()
            assert match_webhook_environment(body, prod_sig) == "production"
            assert match_webhook_environment(body, sandbox_sig) == "sandbox"
            assert match_webhook_environment(body, "0" * 64) is None
            assert match_webhook_environment(body, None) is None

    def test_an_unconfigured_environment_can_never_match(self):
        from backend.app.delivery.uber.environments import match_webhook_environment

        body = b"{}"
        empty_sig = hmac.new(b"", body, hashlib.sha256).hexdigest()
        with self._settings(UBER_EATS_SANDBOX_CLIENT_SECRET=""):
            assert match_webhook_environment(body, empty_sig) is None

    def test_adapter_reports_the_verified_environment(self):
        body = b'{"event_id": "e"}'
        with self._settings():
            sig = hmac.new(b"sandbox-secret", body, hashlib.sha256).hexdigest()
            adapter = UberEatsAdapter()
            assert adapter.verified_webhook_environment(body, {"x-uber-signature": sig}) == "sandbox"
            assert adapter.verify_webhook_signature(body, {"x-uber-signature": sig}) is True
