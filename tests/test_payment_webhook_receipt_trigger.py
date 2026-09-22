"""
_mark_order_paid() (routes_payments.py) receipt trigger — the digital
WhatsApp receipt (backend/app/receipts/whatsapp_receipt.py) must be sent
exactly once per order transition to PAID, and NOT again on a duplicate
webhook delivery (order already PAID). Uses the same FakeDB/FakeRequest
pattern as tests/test_payment_webhook_amount_validation.py.
"""
import urllib.parse
import uuid
from unittest.mock import AsyncMock, patch

from backend.app.api.v1.routes_payments import payfast_webhook
from backend.app.payments.payfast import _build_signature as payfast_sign


class FakeRequest:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {}

    async def body(self) -> bytes:
        return self._body


class FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


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


def make_business(**overrides):
    class Biz:
        pass
    b = Biz()
    b.id = uuid.uuid4()
    b.payment_webhook_secret = None
    b.whatsapp_phone_number_id = "1234567890"
    b.is_whatsapp_enabled = True
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def make_order(business_id, customer_id, **overrides):
    class Order:
        pass
    o = Order()
    o.id = uuid.uuid4()
    o.business_id = business_id
    o.order_number = "BO-000001"
    o.payment_status = "UNPAID"
    o.payment_required = True
    o.payment_reference = None
    o.total_cents = 9000
    o.currency = "ZAR"
    o.status = "NEW"
    o.customer_id = customer_id
    o.paid_at = None
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


def make_customer(**overrides):
    class Cust:
        pass
    c = Cust()
    c.id = uuid.uuid4()
    c.wa_id = "27821234567"
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def _payfast_body(order_id, passphrase="secret"):
    params = {"m_payment_id": str(order_id), "payment_status": "COMPLETE", "amount_gross": "90.00", "pf_payment_id": "pf_x"}
    signed = dict(params)
    signed["signature"] = payfast_sign(params, passphrase)
    return urllib.parse.urlencode(signed).encode("utf-8")


async def test_receipt_sent_exactly_once_on_paid_transition_not_on_duplicate():
    business = make_business()
    customer = make_customer()
    order = make_order(business.id, customer.id)
    body = _payfast_body(order.id)

    with patch("backend.app.api.v1.routes_payments.decrypt_credential", return_value="secret"), \
         patch("backend.app.receipts.whatsapp_receipt.send_whatsapp_receipt", new_callable=AsyncMock) as mock_receipt, \
         patch("backend.app.bot.whatsapp_sender.send_notification_message", new_callable=AsyncMock):
        mock_receipt.return_value = True

        # First delivery: UNPAID -> PAID.
        db1 = FakeDB([
            FakeResult(scalar=business),  # _load_business
            FakeResult(scalar=order),     # select(Order) in _mark_order_paid
            FakeResult(scalar=None),      # award_loyalty_points_if_enabled: select(LoyaltyProgram) — none configured
            FakeResult(scalar=customer),  # select(Customer) for wa_id
            FakeResult(scalar=business),  # select(Business) for phone_number_id
        ])
        result = await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db1)

        assert result == {"received": True}
        assert order.payment_status == "PAID"
        mock_receipt.assert_awaited_once()
        args, kwargs = mock_receipt.call_args
        assert args[1] is order
        assert args[2] is business
        assert kwargs.get("customer") is customer

        # Second delivery of the SAME webhook (order already PAID): the
        # `order.payment_status == "PAID"` early-return in _mark_order_paid
        # must short-circuit before the customer/business lookups even run.
        db2 = FakeDB([
            FakeResult(scalar=business),  # _load_business
            FakeResult(scalar=order),     # select(Order) — already PAID
        ])
        await payfast_webhook(business_id=business.id, request=FakeRequest(body), db=db2)

        assert order.payment_status == "PAID"
        mock_receipt.assert_awaited_once()  # still just once — no duplicate send
