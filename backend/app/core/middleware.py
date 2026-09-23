"""
Middleware — correlation IDs, request logging, timing, token masking.
Every request gets a correlation_id (from header or generated).
Propagated through logs and into audit_events.

Model 1 addition: TokenMaskingFilter masks access tokens and other
credentials in log output. It is attached not only to this app's own
loggers but to the third-party loggers that actually emit
credential-bearing URLs (httpx, uvicorn.access) — see
_THIRD_PARTY_CREDENTIAL_LOGGERS and setup_logging() below.

BUG-0085 (2026-09-24): the same filter also masks customer PII — `wa_id`
(the customer's own WhatsApp phone number) and related fields — wherever
they appear as a logging parameter, by parameter NAME rather than by
call site. See _PII_ARG_KEYS below.
"""

import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("nextgen")

# Context var for correlation ID — accessible anywhere in the request lifecycle
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")

# Regex to find Bearer tokens in log output
_BEARER_PATTERN = re.compile(
    r"(Bearer\s+)([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)
_GENERIC_TOKEN_PATTERN = re.compile(
    r"(token[\"':\s=]+)([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)


# BUG-0075. Credential-bearing QUERY PARAMETERS, which neither pattern above
# matches: _BEARER_PATTERN needs a literal "Bearer ", and _GENERIC_TOKEN_
# PATTERN needs the substring "token" — absent from "client_secret=" entirely,
# and followed by "?" rather than a quote/colon/space/equals in
# "access_token?client_id=".
#
# The value class excludes "%" deliberately. A LogRecord's msg is usually a
# %-format TEMPLATE, and this codebase has four real call sites shaped like
# "... has_token=%s ..." / "... token=%s" (routes_webhook.py,
# whatsapp_sender.py, admin routes). Letting the value class eat the "%s"
# would delete a placeholder while record.args still held its argument, and
# logging would then raise "not all arguments converted during string
# formatting" at render time — a masking filter must never be able to break
# logging itself. Real credential values never contain "%" unless they are
# percent-encoded, and _mask_record_msg below is a second guard.
#
# Masked COMPLETELY rather than in the partial first6...last4 style used for
# bearer tokens above: META_APP_SECRET is also the HMAC key for Meta webhook
# signature verification, so revealing any part of it is a platform-wide
# forgery risk, and an OAuth authorization code is a single-use credential
# that is not useful to see partially. The parameter NAME is kept so the log
# line stays diagnostic.
_SECRET_PARAM_PATTERN = re.compile(
    r"\b(client_secret|api_key|apikey|password|[A-Za-z0-9_]*token)="
    r'([^&\s%"\']+)',
    re.IGNORECASE,
)

# An OAuth authorization code. Length-gated, and deliberately does NOT match
# "error_code="/"error_subcode="/"status_code=" (no word boundary before
# "code" there), so Meta's short numeric diagnostic codes — code=190,
# code=100 — stay readable, since in those messages they are the whole point.
_OAUTH_CODE_PATTERN = re.compile(
    r"\b(code)=([A-Za-z0-9_\-\.]{20,})",
    re.IGNORECASE,
)

_REDACTED = "***"

# BUG-0076. The public receipt token travels in the URL PATH, not a query
# parameter — `GET /v1/r/<token>` — so no name=value pattern can ever catch
# it, and stripping query strings from the access-log format would not help
# either. That token is the ENTIRE authorization for a real customer's order
# data (bearer-equivalent, see routes_receipt_public.py), so the path segment
# itself is redacted. Length-gated so ordinary short paths are untouched;
# real tokens are ~43 url-safe base64 characters (secrets.token_urlsafe(32)).
_RECEIPT_PATH_PATTERN = re.compile(r"(/r/)([A-Za-z0-9_\-]{20,})")


# Third-party loggers that emit credential-bearing URLs. These are the ACTUAL
# emitters of both leaks — no call site in this codebase logs them:
#   httpx         — BUG-0075, logs the full request URL (client_secret, code)
#   uvicorn.access — BUG-0076, logs the full request line (receipt token in
#                    the path, SSE ?token= in the query string)
#
# uvicorn.access is declared by uvicorn's own LOGGING_CONFIG with
# propagate=False and its own handler, which is exactly why a filter on the
# ROOT logger's handlers never saw one of its records.
#
# DELIVERY MODULE (Uber Eats) — no separate attachment needed. httpx emits
# ALL of its "HTTP Request: %s %s ..." summary lines through exactly one
# process-wide logger named "httpx" (confirmed against the installed httpx
# 0.28.1: `logging.getLogger("httpx")` in `httpx/_client.py`), regardless of
# which `httpx.AsyncClient` instance made the call — so the shared client in
# `backend/app/delivery/uber/adapter.py` is already covered by the "httpx"
# entry below, the same as every payment-provider/Meta httpx call. Two
# things keep Uber's own credentials out of this specific logger's output
# even before masking: (1) `uber/auth.py` sends `client_secret` as POST
# BODY form data, never a URL query parameter, and httpx's summary line
# logs only the method + URL, never the body; (2) every Uber API call
# authenticates via an `Authorization: Bearer <token>` HEADER, also never
# logged by httpx's summary line. `_SECRET_PARAM_PATTERN`'s existing
# `client_secret` name and `_BEARER_PATTERN` above are still applied to
# every record from this logger regardless, as defence in depth against a
# future call shape (or a raised exception whose message embeds a URL) that
# does put one of these in a URL or an application log line.
_THIRD_PARTY_CREDENTIAL_LOGGERS = ("httpx", "uvicorn.access")


# BUG-0085 (broader finding, 2026-09-24): `wa_id` is the customer's own
# WhatsApp phone number verbatim (see shared/models/customer.py
# Customer.wa_id — "WhatsApp user identifier (phone number format)"), and it
# is logged, UNCONDITIONALLY, on the ordinary message-handling path
# (bot/pipeline.py, api/v1/routes_webhook.py, bot/whatsapp_sender.py,
# worker.py, bot/meta_errors.py) at INFO/WARNING level — not gated by DEBUG
# at all, unlike the SQL-echo leak BUG-0085 was originally filed for. That's
# roughly two dozen call sites on the platform's single highest-volume code
# path (every inbound/outbound WhatsApp message).
#
# Editing every call site individually is the higher-regression-risk fix —
# it's exactly the pipeline the tests/replay suite exists to protect,
# and hand-editing ~25 log lines across 5 files for a purely-cosmetic log
# change is a lot of needless diff surface for zero behavioural change. The
# lower-risk fix is the same one already used for tokens/secrets above:
# mask centrally, in the filter, based on the PARAMETER NAME the message
# template uses — regardless of which variable the caller actually passed
# (deliberately conservative: a mislabeled non-PII value next to a
# PII-shaped key name gets masked too, which is the safe direction to err).
#
# NOTE: `phone_number_id` is Meta's opaque WABA phone-number-configuration
# ID (an internal identifier, not a phone number — see graph_client.py) and
# is intentionally EXCLUDED here; masking it would remove a genuinely useful,
# non-PII diagnostic value for no privacy benefit. `phone_number` /
# `phone_number_e164` / `customer_phone` / `msisdn` are not currently used as
# logged parameter names anywhere in this codebase (grep-confirmed,
# 2026-09-24) but are included for when they are.
_PII_ARG_KEYS = frozenset({
    "wa_id",
    "customer_wa_id",
    "phone_number",
    "phone_number_e164",
    "customer_phone",
    "msisdn",
})

# Defence-in-depth companion to the position-based masking below: catches the
# same key names when they appear as already-rendered `name=value` text
# (e.g. a future f-string, or an exception message that happens to embed
# one) rather than as a separate %-style logging argument. Value class is
# digits/+/- only (E.164-shaped, no trailing whitespace) so this can't
# accidentally eat an unrelated token/UUID that merely follows one of these
# key names, or swallow trailing punctuation/whitespace into the match.
_PII_PARAM_PATTERN = re.compile(
    r"\b(wa_id|customer_wa_id|phone_number_e164|phone_number|customer_phone|msisdn)="
    r"([0-9+][0-9\-]{4,20})",
    re.IGNORECASE,
)


def _mask_phone(value):
    """Partially mask a phone-number-shaped value, keeping only the last 4 characters."""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # noqa: BLE001 — never let logging raise
            return value
    if len(value) <= 4:
        return _REDACTED
    return f"{_REDACTED}{value[-4:]}"


def _pii_arg_positions(template: str) -> set[int]:
    """
    Zero-based positions of %s/%r placeholders in `template` immediately
    preceded by a PII-shaped `key=` label (e.g. "wa_id=%s").

    Pure text scanning over the TEMPLATE only — never touches the actual
    argument values — so it works before record.args has been formatted in,
    and correctly tracks placeholder index even when the same key name
    appears more than once.
    """
    positions: set[int] = set()
    i = 0
    arg_index = 0
    while i < len(template):
        if template[i] == "%" and i + 1 < len(template) and template[i + 1] in "sr":
            preceding = template[:i]
            m = re.search(r"(\w+)\s*=\s*$", preceding)
            if m and m.group(1).lower() in _PII_ARG_KEYS:
                positions.add(arg_index)
            arg_index += 1
            i += 2
            continue
        i += 1
    return positions


def _mask_string(s: str) -> str:
    """
    Mask every credential pattern this filter knows about.

    Full redactions run FIRST, so a value that matches both a full and a
    partial rule ends up fully redacted rather than partially revealed.
    """
    s = _SECRET_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", s)
    s = _OAUTH_CODE_PATTERN.sub(lambda m: f"{m.group(1)}={_REDACTED}", s)
    s = _RECEIPT_PATH_PATTERN.sub(lambda m: f"{m.group(1)}{_REDACTED}", s)
    s = _PII_PARAM_PATTERN.sub(lambda m: f"{m.group(1)}={_mask_phone(m.group(2))}", s)
    s = _BEARER_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
        s,
    )
    s = _GENERIC_TOKEN_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)[:6]}...{m.group(2)[-4:]}",
        s,
    )
    return s


def _mask_record_msg(msg: str, has_args: bool) -> str:
    """
    Mask a LogRecord's msg, refusing to do so if that would break %-format
    rendering.

    When record.args is non-empty, record.msg is a %-format template and the
    number of "%" characters in it is load-bearing: drop one and
    `msg % args` raises TypeError at render time. The patterns above are all
    written not to match a placeholder, but a masking filter that can crash
    logging is worse than one that occasionally under-masks, so this verifies
    the invariant instead of trusting it.
    """
    masked = _mask_string(msg)
    if has_args and masked.count("%") != msg.count("%"):
        return msg
    return masked


def _mask_arg(a):
    """
    Mask one %-style log argument.

    Non-`str` arguments used to be skipped outright, which made this filter a
    complete no-op against the leak it most needed to catch: httpx logs
    `HTTP Request: %s %s ...` with `request.url` as an `httpx.URL` OBJECT, so
    the full code-exchange query string — client_secret and all — sailed
    straight through (BUG-0075).

    A non-str arg is therefore stringified and masked, but the result is only
    substituted back when masking ACTUALLY changed something. That keeps
    %d/%f/%03d formatting working for ordinary ints/floats, which would
    otherwise raise at render time.
    """
    if isinstance(a, str):
        return _mask_string(a)
    if a is None or isinstance(a, (int, float, bool)):
        return a
    try:
        text = str(a)
    except Exception:  # noqa: BLE001 — never let logging raise
        return a
    masked = _mask_string(text)
    return masked if masked != text else a


class TokenMaskingFilter(logging.Filter):
    """
    Log filter that masks access tokens and other credentials before they
    reach log output. Prevents accidental credential leakage in production
    logs.

    Attached in setup_logging() to the root logger's handlers, to this app's
    own loggers, and to third-party loggers known to emit credential-bearing
    URLs (httpx, uvicorn.access). The latter matter because those loggers are
    the actual emitters, and a filter attached to a LOGGER runs inside
    Logger.handle() for every record logged through it, regardless of where
    the handlers live.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Computed from the ORIGINAL template, before any masking below might
        # touch it, so positions always line up with record.args' own order.
        pii_positions: set[int] = set()
        if isinstance(record.msg, str) and isinstance(record.args, tuple) and record.args:
            pii_positions = _pii_arg_positions(record.msg)

        if isinstance(record.msg, str):
            record.msg = _mask_record_msg(record.msg, bool(record.args))
        # Also check args (for %-style formatting)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (_mask_phone(v) if k.lower() in _PII_ARG_KEYS else _mask_arg(v))
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _mask_phone(a) if idx in pii_positions else _mask_arg(a)
                    for idx, a in enumerate(record.args)
                )
            else:
                record.args = _mask_arg(record.args)
        return True


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Assigns a correlation ID to every request.
    - Uses X-Correlation-ID header if provided by the client.
    - Otherwise generates a new UUID.
    - Sets it on response headers for tracing.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        cid = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())[:16]
        correlation_id_ctx.set(cid)
        request.state.correlation_id = cid

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every request with method, path, status, and duration.
    Uses structured logging format.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        cid = getattr(request.state, "correlation_id", "unknown")

        # Skip logging health checks to reduce noise
        if request.url.path not in ("/health", "/ready"):
            logger.info(
                "request_completed",
                extra={
                    "correlation_id": cid,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "client_ip": request.client.host if request.client else None,
                },
            )

        return response


def get_correlation_id() -> str:
    """Get the current request's correlation ID (for use in services/audit)."""
    return correlation_id_ctx.get()


def setup_logging(environment: str = "development") -> None:
    """
    Configure logging with token masking filter.
    Call this during app startup.
    """
    mask_filter = TokenMaskingFilter()

    # Apply to root logger so ALL log output is masked
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(mask_filter)

    # Also apply to our app loggers
    for name in ("nextgen", "nextgen.webhook", "nextgen.bot", "nextgen.outbox"):
        logging.getLogger(name).addFilter(mask_filter)

    # BUG-0075: httpx logs every request as
    # `HTTP Request: GET <full url> "HTTP/1.1 200 OK"` at INFO, and the Meta
    # code-exchange URL legitimately carries client_secret/code as query
    # parameters (that is how Meta's /oauth/access_token endpoint works, so
    # the request shape itself cannot change). Attach the filter to the
    # EMITTING logger rather than relying on a root handler being configured.
    for name in _THIRD_PARTY_CREDENTIAL_LOGGERS:
        logger_obj = logging.getLogger(name)
        if not any(isinstance(f, TokenMaskingFilter) for f in logger_obj.filters):
            logger_obj.addFilter(mask_filter)
        # These loggers commonly set propagate=False and carry their own
        # handlers, which a root-handler filter would never reach.
        for handler in logger_obj.handlers:
            handler.addFilter(mask_filter)
