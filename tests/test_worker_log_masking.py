"""
Souk WhatsApp security review H-2: PII masking must be active on the bot path.

Before: setup_logging() ran only in main.py (API process), never in the ARQ
worker where the bot pipeline runs; and the masking filter was attached to the
`nextgen`/`nextgen.bot` LOGGERS, which does not cover child loggers such as
`nextgen.bot.pipeline`. Raw customer numbers (`to=`) and message text were
logged.
"""
import io
import logging
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from backend.app import worker
from backend.app.core.middleware import TokenMaskingFilter


@pytest.fixture
def bare_root_logger():
    """Simulate the worker process: no root handler configured (arq only configures `arq`)."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    named = [logging.getLogger(n) for n in ("nextgen", "nextgen.webhook", "nextgen.bot", "nextgen.outbox")]
    saved_filters = {lg.name: lg.filters[:] for lg in named}
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for lg in named:
            lg.filters = saved_filters[lg.name]


@pytest.mark.asyncio
async def test_worker_startup_masks_child_logger_pii(bare_root_logger):
    bare_root_logger.handlers = []  # pytest re-adds its capture handlers for the call phase
    with patch.object(worker, "create_async_engine", MagicMock()), \
         patch.object(worker, "async_sessionmaker", MagicMock()):
        await worker.startup({})

    assert bare_root_logger.handlers, "worker startup must install a root handler"
    handler = bare_root_logger.handlers[0]
    assert any(isinstance(f, TokenMaskingFilter) for f in handler.filters)
    stream = io.StringIO()
    handler.setStream(stream)

    # Child loggers: no filter of their own, only the root handler's.
    logging.getLogger("nextgen.bot.pipeline").warning(
        "PIPELINE_START: wa_message_id=%s, phone_number_id=%s, wa_id=%s, type=%s",
        "wamid.X", "1234567890", "27831234567", "text",
    )
    logging.getLogger("nextgen.webhook").warning(
        "WEBHOOK_STATUS_FAILED: msg=%s, to=%s, error=%s", "wamid.Y", "27839876543", "boom",
    )
    logging.getLogger("nextgen.bot.sender").info("Sending image: phone_number_id=%s, to=%s, url=%s",
                                                 "1234567890", "27835550000", "https://x/y.png")
    out = stream.getvalue()
    for raw in ("27831234567", "27839876543", "27835550000"):
        assert raw not in out
    assert "wa_id=***4567" in out
    assert "to=***6543" in out
    assert "to=***0000" in out
    # Meta's opaque phone_number_id is deliberately not masked (diagnostic, not PII).
    assert "phone_number_id=1234567890" in out


@pytest.mark.asyncio
async def test_setup_logging_is_idempotent(bare_root_logger):
    bare_root_logger.handlers = []
    with patch.object(worker, "create_async_engine", MagicMock()), \
         patch.object(worker, "async_sessionmaker", MagicMock()):
        await worker.startup({})
        await worker.startup({})
    assert len(bare_root_logger.handlers) == 1
    assert sum(isinstance(f, TokenMaskingFilter) for f in bare_root_logger.handlers[0].filters) == 1


def test_pipeline_does_not_log_raw_customer_text_or_address():
    """Free text and addresses are logged as lengths only."""
    src = pathlib.Path("backend/app/bot/pipeline.py").read_text(encoding="utf-8")
    assert "msg_text[:" not in src
    assert "norm_text[:" not in src
    assert "text_preview=%r" not in src
    assert "address=%r" not in src


@pytest.fixture
def arq_logger_like_cli():
    """What arq.cli's dictConfig leaves behind: an `arq` logger with its own handler."""
    arq = logging.getLogger("arq")
    saved = (arq.handlers[:], arq.propagate, arq.level)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    arq.handlers = [handler]
    arq.propagate = True
    arq.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        arq.handlers, arq.propagate = saved[0], saved[1]
        arq.setLevel(saved[2])


@pytest.mark.asyncio
async def test_arq_job_start_line_is_redacted_and_printed_once(bare_root_logger, arq_logger_like_cli):
    """Review L-5: arq logs job args (wa_id, message text, payload) as bare reprs."""
    bare_root_logger.handlers = []
    with patch.object(worker, "create_async_engine", MagicMock()), \
         patch.object(worker, "async_sessionmaker", MagicMock()):
        await worker.startup({})
    root_stream = io.StringIO()
    bare_root_logger.handlers[0].setStream(root_stream)

    job_args = "'1234567890', 'wamid.X', '27831234567', 'deliver to 12 Main Road', 'text', {}"
    logging.getLogger("arq.worker").info("%6.2fs → %s(%s)%s", 0.05, "j1:process_whatsapp_message", job_args, "")
    logging.getLogger("arq.worker").info("recording health: %s", "j_complete=1")
    out = arq_logger_like_cli.getvalue()
    assert "27831234567" not in out and "12 Main Road" not in out
    assert "j1:process_whatsapp_message" in out and "chars of job arguments redacted" in out
    assert "recording health: j_complete=1" in out  # other arq lines untouched
    assert root_stream.getvalue() == ""  # not duplicated through the root handler
