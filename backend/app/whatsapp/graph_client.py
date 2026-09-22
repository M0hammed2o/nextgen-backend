"""
Meta Graph API client for WhatsApp Embedded Signup (Wave 0A / Milestone C).

Every Graph HTTP call the Embedded Signup flow needs is isolated in this one
class so a path/param change never touches connection_service.py's business
logic — exactly the "small client class" the task brief asks for. No call
in this file is exercised against live Meta in this session (ABSOLUTE
LIMITS: no network calls); every path below was researched against Meta's
developer docs as of 2026-09-21 and is marked "verify against Meta docs"
wherever this session could not directly confirm it. Tested with mocked
httpx (see tests/test_whatsapp_graph_client.py).

NEVER log an access token, app secret, or authorization code — every method
here takes a token as a plain string and passes it straight into an
Authorization header; callers must not log payloads/headers.

That invariant is enforced at two levels, because this file alone cannot
hold it: no call site here logs a URL or params (the only logger call below
reports `path`, never the query string), AND TokenMaskingFilter is attached
to the "httpx" logger itself, since httpx logs the FULL request URL at INFO
underneath this class — which is how META_APP_SECRET and the OAuth code
previously reached production logs (BUG-0075).
"""

import logging

import httpx

from backend.app.core.config import get_settings

logger = logging.getLogger("nextgen.whatsapp.graph")


class GraphAPIError(Exception):
    """
    A Graph API call failed. Carries Meta's own error code/subcode when the
    response body was parseable, so callers can act on specific failures
    (e.g. code 190 = invalid/expired token) without re-parsing text.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: int | None = None,
        error_subcode: int | None = None,
        raw: dict | None = None,
    ):
        self.status_code = status_code
        self.error_code = error_code
        self.error_subcode = error_subcode
        self.raw = raw or {}
        super().__init__(message)

    @property
    def is_invalid_token(self) -> bool:
        """Graph error code 190 = the access token is invalid or expired."""
        return self.error_code == 190

    @property
    def is_already_registered(self) -> bool:
        """
        Heuristic for "this phone number is already registered" on a
        /register call — Meta does not document one single stable
        code/subcode for this across API versions. VERIFY AGAINST META DOCS
        before relying on this in production; until then callers must treat
        a False here as "genuinely failed", never silently succeed.
        """
        text = (self.raw.get("message") or str(self)).lower()
        return "already" in text and "regist" in text


class MetaGraphClient:
    """Thin wrapper over the subset of Graph API calls Embedded Signup needs."""

    def __init__(
        self,
        base_url: str | None = None,
        version: str | None = None,
        timeout: float = 15.0,
    ):
        settings = get_settings()
        self._base_url = (base_url or settings.META_API_BASE_URL).rstrip("/")
        self._version = version or settings.META_API_VERSION
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{self._version}/{path.lstrip('/')}"

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise GraphAPIError(f"Meta Graph API timeout calling {path}") from exc
        except Exception as exc:
            raise GraphAPIError(f"Meta Graph API request failed calling {path}: {exc!r}") from exc

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            message = err.get("message") or (resp.text or "")[:400] or f"Graph API error {resp.status_code}"
            logger.warning(
                "Graph API error: path=%s status=%d code=%s subcode=%s",
                path, resp.status_code, err.get("code"), err.get("error_subcode"),
            )
            raise GraphAPIError(
                message,
                status_code=resp.status_code,
                error_code=err.get("code"),
                error_subcode=err.get("error_subcode"),
                raw=err,
            )
        return data if isinstance(data, dict) else {}

    # ── Step: exchange authorization code for a token ───────────────────
    async def exchange_code_for_token(self, code: str) -> dict:
        """
        GET /{version}/oauth/access_token?client_id&client_secret&code

        VERIFY AGAINST META DOCS — Embedded Signup v4's documented
        code-exchange path as researched 2026-09-21. Returns
        {"access_token": ..., "token_type": ..., "expires_in": <optional>}.
        """
        settings = get_settings()
        params = {
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "code": code,
        }
        # BUG-0075. Log ONLY the path — never `params`, never the built URL.
        # Meta's /oauth/access_token really is a GET with client_id/
        # client_secret/code as query parameters, so the request shape above
        # is fixed and correct; what must never happen is that query string
        # reaching log output. This module makes no such log call (verified:
        # the only other logger call here reports `path` alone), but httpx's
        # own logger emits `HTTP Request: GET <full url> "HTTP/1.1 200 OK"`
        # at INFO for every request it makes, which is where the secret
        # actually leaked. TokenMaskingFilter is attached directly to the
        # "httpx" logger by setup_logging() and redacts client_secret=/code=
        # there; this line exists so the exchange is still observable from
        # our own logs without depending on that.
        logger.debug(
            "Meta code exchange: GET %s (client_secret and code redacted)",
            self._url("oauth/access_token"),
        )
        return await self._request("GET", "oauth/access_token", params=params)

    # ── Step: confirm the token really has access to the claimed assets ─
    # (never trust waba_id/phone_number_id the browser reports without
    # confirming the exchanged token can actually read them)
    async def get_waba(self, waba_id: str, access_token: str) -> dict:
        """GET /{waba_id}?fields=id,name"""
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"fields": "id,name"}
        return await self._request("GET", waba_id, headers=headers, params=params)

    async def get_phone_number(self, phone_number_id: str, access_token: str) -> dict:
        """
        GET /{phone_number_id}?fields=display_phone_number,verified_name,
                                       quality_rating,code_verification_status
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "fields": "display_phone_number,verified_name,quality_rating,code_verification_status"
        }
        return await self._request("GET", phone_number_id, headers=headers, params=params)

    # ── Step: subscribe / unsubscribe our app's webhooks to this WABA ───
    async def subscribe_app(self, waba_id: str, access_token: str) -> dict:
        """POST /{waba_id}/subscribed_apps"""
        headers = {"Authorization": f"Bearer {access_token}"}
        return await self._request("POST", f"{waba_id}/subscribed_apps", headers=headers)

    async def unsubscribe_app(self, waba_id: str, access_token: str) -> dict:
        """DELETE /{waba_id}/subscribed_apps — used on disconnect."""
        headers = {"Authorization": f"Bearer {access_token}"}
        return await self._request("DELETE", f"{waba_id}/subscribed_apps", headers=headers)

    # ── Step: register the phone number for Cloud API messaging ─────────
    async def register_phone(self, phone_number_id: str, access_token: str, pin: str) -> dict:
        """
        POST /{phone_number_id}/register {messaging_product: whatsapp, pin}

        Callers should catch GraphAPIError and check `.is_already_registered`
        to tolerate re-registering an already-registered number (idempotent
        resume of a partially-completed connect flow) — see
        connection_service._register_phone.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        payload = {"messaging_product": "whatsapp", "pin": pin}
        return await self._request("POST", f"{phone_number_id}/register", headers=headers, json=payload)

    # ── Message send (used by the business-connection send path and the
    #    owner-facing POST /test-message route) ──────────────────────────
    async def send_message(self, phone_number_id: str, access_token: str, payload: dict) -> dict:
        """POST /{phone_number_id}/messages — payload must set type/body etc."""
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        body = dict(payload)
        body.setdefault("messaging_product", "whatsapp")
        return await self._request("POST", f"{phone_number_id}/messages", headers=headers, json=body)
