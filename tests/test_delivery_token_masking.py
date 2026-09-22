"""
Empirical proof that the EXISTING `TokenMaskingFilter` (backend/app/core/
middleware.py) already masks Uber Eats credential shapes (client_secret,
Bearer operational tokens) — see that module's updated docstring for why no
regex change was needed (httpx's single process-wide "httpx" logger is
already in `_THIRD_PARTY_CREDENTIAL_LOGGERS`, and Uber's client_secret is
sent as POST body form data / the token as an Authorization header, neither
of which httpx's own summary log line includes). This test proves the
masking rules themselves are sufficient for the shapes that COULD appear in
an application log line, rather than just asserting by inspection.
"""

import logging

from backend.app.core.middleware import TokenMaskingFilter, _mask_string


class TestUberCredentialShapesAreMasked:
    def test_client_secret_query_style_value_is_fully_redacted(self):
        masked = _mask_string(
            "requesting https://auth.uber.com/oauth/v2/token?client_id=abc&client_secret=SUPERSECRETVALUE123"
        )
        assert "SUPERSECRETVALUE123" not in masked
        assert "client_secret=***" in masked

    def test_uber_bearer_operational_token_is_partially_masked(self):
        token = "A" * 40
        masked = _mask_string(f"Authorization: Bearer {token}")
        assert token not in masked
        assert masked.startswith("Authorization: Bearer AAAAAA...")

    def test_filter_masks_a_real_log_record_with_percent_args(self):
        filt = TokenMaskingFilter()
        record = logging.LogRecord(
            name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
            msg="HTTP Request: POST %s", args=("https://auth.uber.com/oauth/v2/token?client_secret=LEAKED_VALUE",),
            exc_info=None,
        )
        filt.filter(record)
        rendered = record.msg % record.args
        assert "LEAKED_VALUE" not in rendered
        assert "client_secret=***" in rendered

    def test_httpx_logger_is_in_the_credential_logger_list(self):
        from backend.app.core.middleware import _THIRD_PARTY_CREDENTIAL_LOGGERS

        assert "httpx" in _THIRD_PARTY_CREDENTIAL_LOGGERS

    def test_uber_signature_header_value_itself_is_not_a_recognised_credential_pattern(self):
        """
        X-Uber-Signature is a per-request HMAC digest, not a reusable
        credential — confirming it is deliberately NOT swept up by the
        generic 'token' pattern (which would be over-eager and mask useful
        diagnostic hex digests unrelated to any secret).
        """
        masked = _mask_string("X-Uber-Signature: " + "a" * 64)
        assert "a" * 64 in masked
