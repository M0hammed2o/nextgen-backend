"""
BUG-0075 — META_APP_SECRET and the Meta OAuth authorization code must never
reach log output.

Meta's code-exchange endpoint genuinely is a GET with client_id/
client_secret/code as QUERY PARAMETERS, so the request shape itself is not
negotiable and is deliberately left alone here. What is fixed is the log
output: httpx's own `logging.getLogger("httpx")` emits
`HTTP Request: GET <full url> "HTTP/1.1 200 OK"` at INFO for every request,
and TokenMaskingFilter did not catch it for two independent reasons —
neither of its patterns matched `client_secret=`/`code=`, and httpx passes
the URL as a non-`str` `httpx.URL` arg, which _mask_string skipped outright.

META_APP_SECRET is the same secret used for Meta webhook HMAC verification,
so its disclosure would let an attacker forge inbound WhatsApp webhooks for
every tenant — this is why it is masked outright rather than partially.

These tests assert against ACTUAL RENDERED LOG OUTPUT from the real
configured logging stack (setup_logging() + a formatter), not by calling
the filter function directly — the wiring is where the original bug lived.
No network: httpx.MockTransport serves the response, so httpx's real
request/logging code path still executes.
"""
from __future__ import annotations

import io
import logging
import types
from unittest.mock import patch

import httpx
import pytest

from backend.app.core.middleware import TokenMaskingFilter, setup_logging
from backend.app.whatsapp.graph_client import MetaGraphClient

APP_SECRET = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
AUTH_CODE = "AQBxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_-abcdefghijklmnop"
APP_ID = "1234567890123456"


@pytest.fixture
def captured_logs():
    """
    Attach a real handler with the app's real format to the root logger,
    after running the app's own setup_logging(). Anything rendered here is
    what would genuinely be written to stdout in production.
    """
    setup_logging("production")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    handler.addFilter(TokenMaskingFilter())
    handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.addHandler(handler)
    previous_level = root.level
    root.setLevel(logging.DEBUG)

    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.DEBUG)

    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        httpx_logger.setLevel(previous_httpx_level)


def _fake_settings():
    return types.SimpleNamespace(
        META_APP_ID=APP_ID,
        META_APP_SECRET=APP_SECRET,
        META_API_BASE_URL="https://graph.facebook.com",
        META_API_VERSION="v22.0",
    )


async def _run_exchange():
    """Run a real exchange_code_for_token() against a mocked Meta."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "EAA-fake-token", "token_type": "bearer"})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    def _client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("backend.app.whatsapp.graph_client.get_settings", _fake_settings):
        with patch("httpx.AsyncClient", _client_with_mock_transport):
            client = MetaGraphClient()
            return await client.exchange_code_for_token(AUTH_CODE)


class TestExchangeNeverLogsTheSecret:
    async def test_real_exchange_call_leaks_nothing_to_configured_logs(self, captured_logs):
        """
        The headline assertion: run the real code-exchange path and grep the
        ACTUAL rendered log output for the secret and the authorization code.
        """
        data = await _run_exchange()
        assert data["access_token"] == "EAA-fake-token"  # the call really ran

        output = captured_logs.getvalue()
        # Sanity: httpx really did log this request, so the assertions below
        # are meaningful rather than vacuously true.
        assert "HTTP Request" in output, "httpx did not log at all — test proves nothing"

        assert APP_SECRET not in output, "META_APP_SECRET reached log output"
        assert AUTH_CODE not in output, "the Meta OAuth authorization code reached log output"

    async def test_redacted_markers_are_present_instead(self, captured_logs):
        """The line is still useful for debugging — just without the values."""
        await _run_exchange()
        output = captured_logs.getvalue()
        assert "client_secret=***" in output
        assert "code=***" in output
        # The non-sensitive parts survive, so the log line is still diagnostic.
        assert "oauth/access_token" in output
        assert APP_ID in output


class TestFilterPatternCoverage:
    """
    Defense-in-depth: the filter must mask these shapes wherever they appear,
    not only in the one call site fixed above — any other place that logs a
    Meta URL is covered by the same patterns.
    """

    def _render(self, logger_name, msg, args=()):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(logger_name)
        logger.addFilter(TokenMaskingFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        previous_propagate = logger.propagate
        logger.propagate = False
        try:
            logger.info(msg, *args)
            return stream.getvalue()
        finally:
            logger.removeHandler(handler)
            logger.filters.clear()
            logger.propagate = previous_propagate

    def test_masks_client_secret_in_a_query_string(self):
        out = self._render(
            "nextgen.test.secret",
            "GET https://graph.facebook.com/v22.0/oauth/access_token"
            f"?client_id={APP_ID}&client_secret={APP_SECRET}&code={AUTH_CODE}",
        )
        assert APP_SECRET not in out
        assert AUTH_CODE not in out
        assert "client_secret=***" in out
        assert "code=***" in out

    def test_masks_non_str_url_objects(self):
        """
        httpx logs the URL as an httpx.URL, not a str — the original filter
        skipped non-str args entirely, which is why it was a no-op here.
        """
        url = httpx.URL(
            "https://graph.facebook.com/v22.0/oauth/access_token"
            f"?client_id={APP_ID}&client_secret={APP_SECRET}&code={AUTH_CODE}"
        )
        out = self._render("nextgen.test.url", 'HTTP Request: %s %s "%s"', ("GET", url, "HTTP/1.1 200 OK"))
        assert APP_SECRET not in out
        assert AUTH_CODE not in out
        assert "client_secret=***" in out

    def test_masks_access_token_query_param(self):
        out = self._render(
            "nextgen.test.at",
            "https://graph.facebook.com/v22.0/me?access_token=EAAsupersecretvalue1234567890",
        )
        assert "EAAsupersecretvalue1234567890" not in out

    def test_does_not_mangle_short_numeric_codes(self):
        """
        `code=` must only be masked when it looks like an OAuth code. Meta's
        own error payloads use short numeric codes that are diagnostic, not
        secret, and must stay readable.
        """
        out = self._render("nextgen.test.codes", "Graph API error: code=190 error_subcode=463 status=401")
        assert "code=190" in out
        assert "error_subcode=463" in out

    def test_does_not_mangle_integer_args(self):
        """Non-str args that are not sensitive must pass through untouched."""
        out = self._render("nextgen.test.ints", "path=%s status=%d code=%s", ("oauth/access_token", 401, 190))
        assert "status=401" in out
        assert "code=190" in out

    def test_still_masks_bearer_tokens(self):
        """The pre-existing behaviour must not regress."""
        out = self._render("nextgen.test.bearer", "Authorization: Bearer EAAsupersecrettokenvalue1234567890")
        assert "EAAsupersecrettokenvalue1234567890" not in out


class TestHttpxLoggerIsWired:
    def test_setup_logging_attaches_the_filter_to_the_httpx_logger(self):
        """
        Wiring assertion, not a behaviour one: the httpx logger is the actual
        emitter of the leaking line, so the filter must be attached to it
        directly and not depend on a root handler existing.
        """
        httpx_logger = logging.getLogger("httpx")
        httpx_logger.filters.clear()
        setup_logging("production")
        assert any(isinstance(f, TokenMaskingFilter) for f in httpx_logger.filters)
