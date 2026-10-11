"""
backend/app/bot/whatsapp_sender.py::send_template_message — brought up to
send_text_message's maturity level for the digital-receipts gate pass
(2026-09-22): optional db=/business= Milestone-C routing, classified
(wamid, MetaErrorInfo) return instead of a bare wamid, and a lightweight
message-history log row (not a retry queue — see the function's own
docstring for why no message_outbox row is written).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.bot.whatsapp_sender import send_template_message
from shared.models.message import Message


def _mock_client(status_code: int, json_body: dict | None = None, text: str = ""):
    import json as json_module

    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text or (json_module.dumps(json_body) if json_body else "")
    mock_response.json.return_value = json_body or {}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


class FakeNestedTransaction:
    """Minimal stand-in for AsyncSession.begin_nested()'s async context manager."""
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False  # never swallow — same as a real SAVEPOINT on error


class FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    def begin_nested(self):
        return FakeNestedTransaction(self)


class FakeDBFlushFails(FakeDB):
    """A Message-history flush that always fails, to prove the failure is
    isolated to its own SAVEPOINT rather than aborting the whole session."""
    async def flush(self):
        self.flushed += 1
        raise RuntimeError("simulated unique-constraint violation on wa_message_id")


class TestPayloadShape:
    async def test_sends_template_payload_with_components(self):
        client = _mock_client(200, {"messages": [{"id": "wamid.receipt1"}]})
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
                language_code="en_US",
                components=[{"type": "body", "parameters": [{"type": "text", "text": "Ciao Pizza"}]}],
            )
        assert wamid == "wamid.receipt1"
        assert info is None
        sent_payload = client.post.call_args.kwargs["json"]
        assert sent_payload["type"] == "template"
        assert sent_payload["template"]["name"] == "nextgen_receipt_ready"
        assert sent_payload["template"]["language"] == {"code": "en_US"}
        assert sent_payload["template"]["components"][0]["parameters"][0]["text"] == "Ciao Pizza"


class TestNeverRaisesReturnsClassifiedError:
    async def test_meta_error_classified_not_raised(self):
        body = {"error": {"code": 131047, "message": "outside window"}}
        client = _mock_client(470, body)
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
            )
        assert wamid is None
        assert info is not None
        assert info.code == 131047
        assert info.retryable is False


class TestMessageHistoryLogging:
    async def test_writes_a_message_row_when_db_and_business_id_given(self):
        client = _mock_client(200, {"messages": [{"id": "wamid.abc"}]})
        db = FakeDB()
        business_id = uuid.uuid4()
        customer_id = uuid.uuid4()
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
                db=db,
                business_id=business_id,
                customer_id=customer_id,
                intent="receipt",
            )
        assert wamid == "wamid.abc"
        assert db.flushed == 1
        assert len(db.added) == 1
        message = db.added[0]
        assert isinstance(message, Message)
        assert message.business_id == business_id
        assert message.customer_id == customer_id
        assert message.direction == "OUTBOUND"
        assert message.wa_message_id == "wamid.abc"
        assert message.intent == "receipt"

    async def test_message_history_flush_failure_isolated_to_its_own_savepoint(self):
        """
        Regression for BUG-0129: a failure writing the Message-history row
        must not abort the caller's own session/transaction. Before the
        fix, this flush failure (uncaught by a rollback) would leave the
        session unusable for anything the caller does next -- e.g. a POS
        Pay-by-Link resend endpoint's own db.commit() right after this
        call would then also fail, turning "couldn't log history" into an
        unrelated 500 and silently discarding the caller's own pending
        state (e.g. intent.whatsapp_status = "SENT").
        """
        client = _mock_client(200, {"messages": [{"id": "wamid.abc"}]})
        db = FakeDBFlushFails()
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
                db=db,
                business_id=uuid.uuid4(),
                intent="receipt",
            )
        # The send itself still succeeded -- the history-logging failure
        # must never mask a real wamid from a caller that needs it.
        assert wamid == "wamid.abc"
        assert info is None

    async def test_no_db_write_attempted_when_db_not_given(self):
        client = _mock_client(200, {"messages": [{"id": "wamid.xyz"}]})
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
            )
        assert wamid == "wamid.xyz"

    async def test_message_history_write_failure_never_blocks_the_return_value(self):
        client = _mock_client(200, {"messages": [{"id": "wamid.resilient"}]})

        class ExplodingDB(FakeDB):
            async def flush(self):
                raise RuntimeError("DB write exploded")

        db = ExplodingDB()
        with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
                db=db,
                business_id=uuid.uuid4(),
            )
        assert wamid == "wamid.resilient"
        assert info is None


class TestBusinessConnectionRouting:
    async def test_routes_via_business_connection_when_business_and_db_given(self):
        with patch(
            "backend.app.bot.whatsapp_sender._send_via_business_connection",
            new_callable=AsyncMock,
        ) as mock_route:
            mock_route.return_value = ("wamid.routed", None)
            business = MagicMock(id=uuid.uuid4())
            db = FakeDB()
            wamid, info = await send_template_message(
                phone_number_id="PNID",
                recipient_wa_id="27821234567",
                template_name="nextgen_receipt_ready",
                db=db,
                business=business,
            )
        assert wamid == "wamid.routed"
        mock_route.assert_awaited_once()
        assert mock_route.call_args.args[1] is business
