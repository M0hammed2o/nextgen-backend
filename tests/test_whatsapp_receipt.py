"""
Digital receipt delivery — backend/app/receipts/whatsapp_receipt.py.

This file covers the PURE/mockable pieces: destination resolution priority
(_resolve_whatsapp_destination, _to_wa_id) and resolve_receipt_customer_and_
phone against a stubbed AsyncSession. The full send_digital_receipt
orchestration (idempotency get-or-create, entitlement gating, force_resend,
concurrent duplicate requests, FAILED-is-retryable, template send wiring)
needs a REAL database to honestly prove — a mock cannot prove the row-lock/
unique-constraint idempotency guarantees — so that lives in
tests/integration/test_digital_receipts_realdb.py.

Digital-receipt-sending is entirely template-based since the Commercial V1
gate pass (see this module's own docstring); the old free-form
build_whatsapp_receipt_text()/send_whatsapp_receipt() has been removed, not
left running in parallel.
"""

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from backend.app.receipts.whatsapp_receipt import (
    _resolve_whatsapp_destination,
    _to_wa_id,
    resolve_receipt_customer_and_phone,
)


def _customer(**overrides):
    defaults = dict(id=uuid.uuid4(), wa_id=None, phone_number=None)
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _order(**overrides):
    defaults = dict(id=uuid.uuid4(), customer_id=None, phone_number=None)
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestToWaId:
    def test_strips_leading_plus(self):
        assert _to_wa_id("+27821234567") == "27821234567"

    def test_passthrough_without_plus(self):
        assert _to_wa_id("27821234567") == "27821234567"


class TestResolveWhatsappDestination:
    def test_explicit_phone_takes_priority_over_customer(self):
        customer = _customer(wa_id="27829999999")
        wa_id, phone = _resolve_whatsapp_destination(
            phone_number_e164="+27821234567", customer=customer,
        )
        assert wa_id == "27821234567"
        assert phone == "+27821234567"

    def test_customer_wa_id_used_as_is_no_normalisation(self):
        customer = _customer(wa_id="27821234567")
        wa_id, phone = _resolve_whatsapp_destination(phone_number_e164=None, customer=customer)
        assert wa_id == "27821234567"
        assert phone is None  # wa_id was already known — nothing new to persist

    def test_falls_back_to_customer_phone_number_normalised(self):
        customer = _customer(wa_id=None, phone_number="0821234567")
        wa_id, phone = _resolve_whatsapp_destination(phone_number_e164=None, customer=customer)
        assert wa_id == "27821234567"
        assert phone == "+27821234567"

    def test_unparseable_customer_phone_number_resolves_to_nothing(self):
        customer = _customer(wa_id=None, phone_number="not a number")
        wa_id, phone = _resolve_whatsapp_destination(phone_number_e164=None, customer=customer)
        assert wa_id is None
        assert phone is None

    def test_no_customer_no_explicit_phone_resolves_to_nothing(self):
        wa_id, phone = _resolve_whatsapp_destination(phone_number_e164=None, customer=None)
        assert wa_id is None
        assert phone is None


class _FakeResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    def __init__(self, result):
        self._result = result

    async def execute(self, stmt):
        return self._result


class TestResolveReceiptCustomerAndPhone:
    async def test_explicit_phone_used_when_order_has_no_linked_customer(self):
        order = _order(customer_id=None, phone_number="0821234567")
        db = _FakeDB(_FakeResult(None))
        customer, phone = await resolve_receipt_customer_and_phone(
            db, order, uuid.uuid4(), "+27821234567",
        )
        assert customer is None
        assert phone == "+27821234567"

    async def test_uses_already_linked_customer_never_creates_one(self):
        existing_customer = _customer(wa_id="27821234567")
        order = _order(customer_id=existing_customer.id, phone_number=None)
        db = _FakeDB(_FakeResult(existing_customer))
        customer, phone = await resolve_receipt_customer_and_phone(
            db, order, uuid.uuid4(), None,
        )
        assert customer is existing_customer
        # No phone_number_e164 resolved here — send_digital_receipt itself
        # falls back to customer.wa_id via _resolve_whatsapp_destination.
        assert phone is None

    async def test_falls_back_to_orders_own_captured_phone_number(self):
        order = _order(customer_id=None, phone_number="082 123 4567")
        db = _FakeDB(_FakeResult(None))
        customer, phone = await resolve_receipt_customer_and_phone(
            db, order, uuid.uuid4(), None,
        )
        assert customer is None
        assert phone == "+27821234567"

    async def test_orders_own_phone_number_ignored_when_unparseable(self):
        order = _order(customer_id=None, phone_number="not a real number")
        db = _FakeDB(_FakeResult(None))
        customer, phone = await resolve_receipt_customer_and_phone(
            db, order, uuid.uuid4(), None,
        )
        assert customer is None
        assert phone is None
