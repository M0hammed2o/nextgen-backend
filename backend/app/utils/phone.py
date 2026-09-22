"""
South African mobile number normalisation.

No prior helper of this kind exists anywhere in the bot/pipeline code (see
TODO.md's 2026-09-22 digital-receipts follow-up, which named this exact gap
— "a real phone-number -> WhatsApp wa_id normalisation helper does not
exist anywhere in the bot code"). This is that helper's first half: turning
whatever a human typed into a canonical `+27XXXXXXXXX` form, ONLY when the
input plausibly represents a real SA mobile number. It never guesses — any
input that doesn't clearly fit one of the accepted shapes returns None
rather than a best-effort value, because a receipt silently "sent" to the
wrong invented number is worse than a 422 asking the operator to check it.

This deliberately does NOT attempt to become a general libphony-style
number parser (no new dependency — hand-rolled digit-shape matching is
enough for the one input population this needs to accept: a South African
GSM mobile number, typed by a customer or a till operator).

Converting a normalised +27... string into the digits-only form Meta's
WhatsApp Cloud API expects for a "to" field is a SEPARATE, send-path
concern (see backend/app/receipts/whatsapp_receipt.py::_to_wa_id) — this
module only answers "is this a real-looking SA mobile number", not
"is this a valid WhatsApp recipient".
"""

import re

_STRIP_CHARS = re.compile(r"[\s\-()]")
_MOBILE_PREFIXES = ("6", "7", "8")


def normalize_sa_mobile(raw: str | None) -> str | None:
    """
    Accepts common SA input shapes:
      - "0821234567"       (local, 10 digits)
      - "082 123 4567"     (local, whitespace-separated)
      - "+27821234567"     (international, with +)
      - "27821234567"      (international, without +)
      - any of the above with spaces, hyphens or parentheses as separators

    Returns a canonical "+27XXXXXXXXX" string only when the input plausibly
    represents a real SA mobile number (the digit after the country/trunk
    code is 6, 7 or 8 — the SA mobile range — and the total digit count is
    exactly right). Returns None for anything else: landlines, wrong
    lengths, non-numeric input, empty/missing input. Never raises.
    """
    if not raw or not isinstance(raw, str):
        return None

    cleaned = _STRIP_CHARS.sub("", raw.strip())
    if not cleaned:
        return None

    if cleaned.startswith("+"):
        digits = cleaned[1:]
        if not digits.isdigit():
            return None
        if not digits.startswith("27") or len(digits) != 11:
            return None
        national = digits[2:]
    elif cleaned.isdigit():
        if cleaned.startswith("27") and len(cleaned) == 11:
            national = cleaned[2:]
        elif cleaned.startswith("0") and len(cleaned) == 10:
            national = cleaned[1:]
        else:
            return None
    else:
        return None

    if len(national) != 9 or not national.isdigit():
        return None
    if national[0] not in _MOBILE_PREFIXES:
        return None

    return f"+27{national}"
