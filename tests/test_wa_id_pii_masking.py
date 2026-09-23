"""
BUG-0085 (broader finding): `wa_id` — the customer's own WhatsApp phone
number, verbatim (see shared/models/customer.py Customer.wa_id) — is logged
unconditionally (not gated by DEBUG) on the ordinary WhatsApp message path
(bot/pipeline.py, api/v1/routes_webhook.py, bot/whatsapp_sender.py,
worker.py, bot/meta_errors.py) at INFO/WARNING level.

Rather than edit ~25 call sites on the platform's single highest-volume code
path, the fix is centralised in `TokenMaskingFilter`
(backend/app/core/middleware.py), the same way credential masking already
is. These tests prove that fix empirically against the real call shapes
found in this codebase, and confirm it does NOT over-mask a value that is
genuinely not customer PII (`phone_number_id`, Meta's opaque WABA
configuration ID).
"""

import logging

from backend.app.core.middleware import TokenMaskingFilter, _mask_phone, _mask_string


def _render(msg: str, *args) -> str:
    """Round-trip a %-style log call through the real filter, then render it."""
    filt = TokenMaskingFilter()
    record = logging.LogRecord(
        name="nextgen", level=logging.WARNING, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )
    filt.filter(record)
    return record.getMessage()


class TestWaIdMaskedByParameterPosition:
    def test_real_pipeline_start_call_shape_masks_wa_id_only(self):
        """The exact call shape from bot/pipeline.py PIPELINE_START."""
        rendered = _render(
            "PIPELINE_START: wa_message_id=%s, phone_number_id=%s, wa_id=%s, type=%s",
            "wamid.HBgLMTIzNDU2Nzg5MA==", "123456789012345", "27821234567", "text",
        )
        assert "27821234567" not in rendered
        assert "wa_id=***4567" in rendered
        # phone_number_id is Meta's opaque WABA config ID, not customer PII —
        # must survive untouched for diagnostic value.
        assert "phone_number_id=123456789012345" in rendered
        # wa_message_id (not customer PII either) also survives untouched.
        assert "wa_message_id=wamid.HBgLMTIzNDU2Nzg5MA==" in rendered

    def test_real_webhook_post_message_call_shape(self):
        """The exact call shape from api/v1/routes_webhook.py WEBHOOK_POST_MESSAGE."""
        rendered = _render(
            "WEBHOOK_POST_MESSAGE: wa_message_id=%s, wa_id=%s, type=%s",
            "wamid.ABC123", "27831234567", "text",
        )
        assert "27831234567" not in rendered
        assert "***4567" in rendered

    def test_real_whatsapp_sender_message_sent_call_shape(self):
        """
        The exact call shape from bot/whatsapp_sender.py — note the argument
        actually passed there is wa_message_id (a mislabeled log line, not a
        PII leak), but position-based masking deliberately masks it anyway
        since it's impossible to distinguish from the message template alone.
        This is the intentionally conservative direction: over-masking a
        non-PII value costs nothing; under-masking a real phone number does.
        """
        rendered = _render("Message sent: wa_id=%s, business=%s", "wamid.XYZ999", "biz-1")
        assert "wamid.XYZ999" not in rendered
        assert "business=biz-1" in rendered

    def test_customer_id_and_opted_out_survive_alongside_wa_id(self):
        """The exact call shape from bot/pipeline.py PIPELINE_CUSTOMER."""
        rendered = _render(
            "PIPELINE_CUSTOMER: customer_id=%s, opted_out=%s, wa_id=%s",
            "c1a2b3c4-0000-0000-0000-000000000000", False, "27821234567",
        )
        assert "27821234567" not in rendered
        assert "customer_id=c1a2b3c4-0000-0000-0000-000000000000" in rendered
        assert "opted_out=False" in rendered

    def test_non_pii_numeric_looking_business_id_is_not_masked(self):
        """A field that merely LOOKS numeric but isn't named as PII survives."""
        rendered = _render("PIPELINE_LIMIT_HIT: business_id=%s message limit hit", "555000111")
        assert "business_id=555000111" in rendered


class TestWaIdMaskedInRenderedText:
    """Defence-in-depth: _PII_PARAM_PATTERN catches already-rendered text."""

    def test_fstring_style_embedding_is_still_masked(self):
        wa_id = "27821234567"
        masked = _mask_string(f"unexpected error for wa_id={wa_id} during send")
        assert wa_id not in masked
        assert "wa_id=***4567" in masked

    def test_phone_number_id_text_is_not_masked_by_the_pii_pattern(self):
        """
        'phone_number_id=' must not be caught by the 'phone_number' PII
        pattern — it is Meta's opaque WABA ID, not a customer phone number.
        """
        masked = _mask_string("phone_number_id=123456789012345")
        assert "123456789012345" in masked


class TestMaskPhoneHelper:
    def test_short_value_fully_redacted(self):
        assert _mask_phone("123") == "***"

    def test_normal_value_keeps_last_four(self):
        assert _mask_phone("27821234567") == "***4567"

    def test_non_string_value_is_stringified_and_masked(self):
        assert _mask_phone(27821234567) == "***4567"


class TestExistingTokenMaskingStillWorks:
    """BUG-0075 regression guard: httpx's OWN logger leak must stay fixed."""

    def test_bearer_token_still_partially_masked(self):
        token = "A" * 40
        rendered = _render("Authorization: %s", f"Bearer {token}")
        assert token not in rendered
        assert "Bearer AAAAAA..." in rendered

    def test_client_secret_query_value_still_fully_redacted(self):
        masked = _mask_string(
            "https://graph.facebook.com/oauth/access_token?client_id=1&client_secret=LEAKED_VALUE&code=abc12345678901234567890"
        )
        assert "LEAKED_VALUE" not in masked
        assert "client_secret=***" in masked

    def test_httpx_url_object_argument_still_masked(self):
        """
        The exact BUG-0075 shape: httpx logs `HTTP Request: %s %s` with a
        non-str httpx.URL OBJECT holding the credential-bearing query string.
        """

        class _FakeURL:
            def __str__(self):
                return "https://graph.facebook.com/oauth/access_token?client_secret=LEAKED_VALUE"

        rendered = _render("HTTP Request: %s %s", "GET", _FakeURL())
        assert "LEAKED_VALUE" not in rendered
