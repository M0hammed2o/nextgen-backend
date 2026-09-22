"""
BUG-0076 — public receipt tokens and SSE JWTs must not appear verbatim in
uvicorn's access log.

ROOT CAUSE (re-verified, not assumed): uvicorn's own LOGGING_CONFIG declares
`"uvicorn.access": {"handlers": ["access"], "propagate": False}`. Because it
does not propagate and carries its own handler, the filter that
setup_logging() attaches to the ROOT logger's handlers never sees a single
access-log record. The record itself is
`'%s - "%s %s HTTP/%s" %d' % (client_addr, method, full_path, ...)`, and
`full_path` is path + query string — so `GET /v1/r/<token>` and the SSE
endpoint's `?token=<jwt>` were both written out in full.

WHY A FILTER AND NOT AN ACCESS-LOG FORMAT CHANGE: the receipt token is in
the PATH, not the query string, so stripping query strings from the access
log format would not fix the headline case at all — it only addresses the
SSE half. The filter covers both, and needs no custom uvicorn formatter
class.

WHY THE FILTER IS ATTACHED TO THE LOGGER, NOT JUST ITS HANDLERS: uvicorn
calls logging.config.dictConfig() when its Config is constructed, which
REMOVES each configured logger's existing handlers but leaves logger-level
filters intact (verified empirically). A handler-level filter attached by
setup_logging() would therefore be silently discarded whenever uvicorn
configures logging afterwards; a logger-level filter survives either
ordering.

The first test below runs a REAL uvicorn server and makes REAL HTTP
requests, because the original defect was entirely a wiring defect — calling
the filter function directly would have "passed" against the broken code.
"""
from __future__ import annotations

import asyncio
import copy
import io
import logging
import logging.config
import secrets
import socket
import threading

import httpx
import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG

from backend.app.core.middleware import TokenMaskingFilter, setup_logging

RECEIPT_TOKEN = secrets.token_urlsafe(32)
SSE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwiYnVzaW5lc3NfaWQiOiJhYmMifQ"
    ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def access_log_capture():
    """
    Capture what the uvicorn.access logger actually emits, with the real
    uvicorn logging configuration applied first (propagate=False + its own
    handler — the exact shape that defeated the original filter).
    """
    logging.config.dictConfig(copy.deepcopy(LOGGING_CONFIG))
    setup_logging("production")

    access_logger = logging.getLogger("uvicorn.access")
    assert access_logger.propagate is False, (
        "test precondition lost: uvicorn.access is supposed to be non-propagating"
    )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        access_logger.removeHandler(handler)


class TestRealServerAccessLog:
    """End-to-end: a real uvicorn server, real HTTP requests, real access log."""

    async def test_real_requests_do_not_leak_tokens_into_the_access_log(self):
        from backend.app.main import app

        port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=copy.deepcopy(LOGGING_CONFIG),
            access_log=True,
            lifespan="off",
        )
        # uvicorn.Config.__init__ runs dictConfig; the app module (and so
        # setup_logging) may already have been imported. Re-run setup_logging
        # here to mirror production, where uvicorn configures logging first
        # and the app module is imported immediately afterwards.
        setup_logging("production")

        access_logger = logging.getLogger("uvicorn.access")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        access_logger.addHandler(handler)
        access_logger.setLevel(logging.INFO)

        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.1)
            assert server.started, "uvicorn did not start"

            base = f"http://127.0.0.1:{port}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{base}/v1/r/{RECEIPT_TOKEN}")
                await client.get(f"{base}/v1/business/orders/live/stream?token={SSE_JWT}")
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            access_logger.removeHandler(handler)

        output = stream.getvalue()

        # Sanity: the access log really did record our requests, so the
        # assertions below are not vacuously true.
        assert "/v1/r/" in output, f"access log did not record the receipt request: {output!r}"

        assert RECEIPT_TOKEN not in output, (
            f"public receipt token leaked into the uvicorn access log: {output!r}"
        )
        assert SSE_JWT not in output, (
            f"SSE JWT leaked into the uvicorn access log: {output!r}"
        )


class TestAccessLogRecordShape:
    """
    Deterministic proof against the exact record uvicorn emits, without
    needing a live socket. Mirrors uvicorn.protocols.http's own call:
        logger.info('%s - "%s %s HTTP/%s" %d', client, method, full_path, ver, status)
    """

    def _emit(self, full_path: str) -> str:
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:60347", "GET", full_path, "1.1", 404,
        )
        return full_path

    def test_receipt_token_in_path_is_masked(self, access_log_capture):
        self._emit(f"/v1/r/{RECEIPT_TOKEN}")
        out = access_log_capture.getvalue()
        assert RECEIPT_TOKEN not in out
        assert "/v1/r/***" in out

    def test_sse_jwt_in_query_string_is_masked(self, access_log_capture):
        self._emit(f"/v1/business/orders/live/stream?token={SSE_JWT}")
        out = access_log_capture.getvalue()
        assert SSE_JWT not in out
        assert "token=***" in out

    def test_any_token_bearing_query_param_is_masked_generically(self, access_log_capture):
        """
        Not just known names — the next token-bearing URL added to this
        codebase must be covered without anyone remembering to update a list.
        """
        for param in ("token", "access_token", "id_token", "refresh_token", "auth_token"):
            self._emit(f"/v1/whatever?{param}={SSE_JWT}")
        out = access_log_capture.getvalue()
        assert SSE_JWT not in out

    def test_status_code_still_renders(self, access_log_capture):
        """
        uvicorn's AccessFormatter does int(record.args[4]); masking must not
        turn the status code into a string or the formatter would break.
        """
        self._emit("/v1/health")
        out = access_log_capture.getvalue()
        assert "404" in out
        assert "/v1/health" in out

    def test_ordinary_paths_are_untouched(self, access_log_capture):
        self._emit("/v1/business/orders?status=NEW&limit=50")
        out = access_log_capture.getvalue()
        assert "/v1/business/orders?status=NEW&limit=50" in out


class TestWiring:
    def test_setup_logging_attaches_filter_to_the_uvicorn_access_logger(self):
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.filters.clear()
        setup_logging("production")
        assert any(isinstance(f, TokenMaskingFilter) for f in access_logger.filters), (
            "TokenMaskingFilter is not attached to uvicorn.access — the root-handler "
            "filter alone can never see its records (propagate=False)"
        )

    def test_filter_survives_uvicorn_reconfiguring_logging_afterwards(self):
        """
        dictConfig removes a logger's handlers but keeps its filters, so the
        filter must be attached to the LOGGER. This pins that property: if
        someone later moves the attachment to handlers only, this fails.
        """
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.filters.clear()
        setup_logging("production")
        logging.config.dictConfig(copy.deepcopy(LOGGING_CONFIG))
        assert any(isinstance(f, TokenMaskingFilter) for f in access_logger.filters)


class TestMaskingNeverBreaksLogging:
    """
    A masking filter that can crash logging is worse than one that
    occasionally under-masks.

    These are the EXACT %-format templates already in this codebase. An
    earlier draft of the BUG-0076 pattern matched `has_token=%s` and replaced
    the `%s` with `***`, deleting a placeholder while record.args still held
    five arguments — every one of these call sites then raised "not all
    arguments converted during string formatting" at render time. Caught by
    tests/test_worker_readiness.py; pinned here so it cannot come back.
    """

    REAL_TEMPLATES = [
        (
            "WEBHOOK_GET_ENTRY: path=%s, mode=%r, has_token=%s, has_challenge=%s, client_ip=%s",
            ("/v1/webhook/meta", "subscribe", True, True, "127.0.0.1"),
        ),
        (
            "Meta API send: url=%s, phone_number_id=%s, token=%s",
            ("https://graph.facebook.com/v22.0/123/messages", "123", "present"),
        ),
        (
            "Meta API test-send: url=%s, phone_number_id=%s, token=%s",
            ("https://graph.facebook.com/v22.0/123/messages", "123", "present"),
        ),
        (
            "WhatsApp test-send: business_id=%s, phone_number_id=%s, "
            "token_source=%s, token=%s, to=%s",
            ("biz-1", "123", "business", "present", "2782"),
        ),
    ]

    @pytest.mark.parametrize("template,args", REAL_TEMPLATES)
    def test_real_templates_still_render(self, template, args):
        record = logging.LogRecord(
            name="nextgen.test", level=logging.WARNING, pathname=__file__, lineno=1,
            msg=template, args=args, exc_info=None,
        )
        assert TokenMaskingFilter().filter(record) is True
        # The assertion that matters: this must not raise.
        rendered = record.getMessage()
        assert rendered

    def test_placeholder_count_is_preserved_for_templates(self):
        template = "token=%s secret=%s"
        record = logging.LogRecord(
            name="nextgen.test", level=logging.WARNING, pathname=__file__, lineno=1,
            msg=template, args=("a", "b"), exc_info=None,
        )
        TokenMaskingFilter().filter(record)
        assert record.msg.count("%") == template.count("%")
        assert record.getMessage()

    def test_a_fully_rendered_message_with_no_args_is_still_masked(self):
        """The no-args path (f-strings, .format()) must keep masking normally."""
        record = logging.LogRecord(
            name="nextgen.test", level=logging.WARNING, pathname=__file__, lineno=1,
            msg=f"GET /v1/r/{RECEIPT_TOKEN}?token={SSE_JWT}", args=(), exc_info=None,
        )
        TokenMaskingFilter().filter(record)
        rendered = record.getMessage()
        assert RECEIPT_TOKEN not in rendered
        assert SSE_JWT not in rendered
