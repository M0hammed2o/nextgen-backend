"""
Activate a mapped Uber Eats SANDBOX store for NextGen (operator tool, one store per run).

Sandbox only. Production stores need a server-side SUPER_ADMIN flow instead
(security review 2026-10-29): running this against production would put the
production database URL, JWT keys and Uber client secret on a workstation.

Runs with the sandbox deployment's environment variables loaded. It:

  1. refuses unless the business already has an Uber Eats SANDBOX connection
     with a store id (SUPER_ADMIN registers it first:
     PUT /v1/admin/admin/businesses/{id}/delivery-connections/UBER_EATS);
  2. binds a one-shot listener (127.0.0.1 only), then opens Uber's consent page.
     The MERCHANT signs in there with their own Uber login — use a private
     browser window. This script never asks for, sees or stores a password;
  3. accepts only a redirect whose `state` matches this run, and exchanges the
     single-use code for a merchant token (memory only);
  4. refuses unless that merchant can see the mapped store;
  5. POST /v1/eats/stores/{store_id}/pos_data — is_order_manager=true,
     integrator_store_id=<business id>;
  6. re-reads the store's status with the app's own (cached) client_credentials
     token — 401 before activation — and audits the outcome, success or failure.

The redirect URI must be registered on the Uber app (Developer Dashboard →
Auth → Redirect URIs) exactly as passed in --redirect-uri. If it is not on
this machine (e.g. an https tunnel), point the tunnel at --listen-port.

Usage:
    python scripts/uber_activate_store.py --business-id <uuid> --operator "<your name>"
        [--redirect-uri http://localhost:8766/uber/oauth/callback] [--listen-port 8766]
"""
import argparse
import asyncio
import hmac
import http.server
import os
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.abspath("."))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

try:
    from backend.app.db.session import async_session_factory, get_redis  # noqa: E402
except Exception as exc:  # pydantic ValidationError when the environment isn't loaded
    sys.exit(f"Load the sandbox deployment's environment variables first (DATABASE_URL, REDIS_URL, JWT keys, "
             f"the Uber sandbox client id/secret). Settings failed: {exc.__class__.__name__}")
from backend.app.delivery.errors import DeliveryProviderError, DeliveryProviderTimeout  # noqa: E402
from backend.app.delivery.uber import auth, client, provisioning  # noqa: E402
from backend.app.delivery.uber.environments import SANDBOX, get_environment_config  # noqa: E402
from shared.enums import AuditScope  # noqa: E402
from shared.models.audit import AuditEvent  # noqa: E402
from shared.models.delivery import DeliveryConnection  # noqa: E402

_CONSENT_TIMEOUT_SECONDS = 600  # Uber's code itself expires after 10 minutes


def _mask(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if len(value) > 10 else "…"


class RedirectListener:
    """
    One-shot OAuth redirect receiver on 127.0.0.1. Bound in __init__ (before
    the browser opens). Only a request on `path` whose `state` matches ends the
    wait; anything else gets 400/404 and the wait continues. Every connection
    has a socket timeout and the overall wait is a real-clock deadline.
    """

    def __init__(self, port: int, path: str, state: str):
        self.result: dict = {}
        expected = state.encode()
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 10

            def do_GET(self):  # noqa: N802 - http.server API
                url = urlparse(self.path)
                if url.path != path:
                    return self._reply(404, b"Not found.")
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                if not hmac.compare_digest(params.get("state", "").encode(), expected):
                    return self._reply(400, b"This link does not belong to the activation that is running.")
                listener.result = params
                self._reply(200, b"NextGen received Uber's response. You can close this tab.")

            def _reply(self, status: int, text: bytes):
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(text)

            def log_message(self, *args):  # never echo the code into a log
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        self._server.timeout = 1

    def wait(self, timeout_seconds: float = _CONSENT_TIMEOUT_SECONDS) -> dict:
        deadline = time.monotonic() + timeout_seconds
        try:
            while not self.result and time.monotonic() < deadline:
                self._server.handle_request()
        finally:
            self._server.server_close()
        return self.result


async def _load_connection(business_id: uuid.UUID) -> DeliveryConnection:
    async with async_session_factory() as db:
        connection = (await db.execute(select(DeliveryConnection).where(
            DeliveryConnection.business_id == business_id, DeliveryConnection.provider == "UBER_EATS",
        ))).scalar_one_or_none()
    if connection is None or not connection.external_store_id:
        sys.exit("REFUSED: this business has no Uber Eats connection with a store id. Register it first.")
    if connection.environment != SANDBOX or connection.status != "SANDBOX":
        sys.exit(f"REFUSED: only SANDBOX/sandbox connections can be activated here "
                 f"(this one is {connection.environment}/{connection.status}).")
    return connection


async def _audit(connection: DeliveryConnection, operator: str, outcome: str, **extra) -> None:
    async with async_session_factory() as db:
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value, business_id=connection.business_id, actor_user_id=None,
            action=f"delivery.uber.store_activation_{outcome}", target_type="delivery_connection",
            target_id=connection.id,
            diff_json={"environment": SANDBOX, "external_store_id": connection.external_store_id,
                       "operator": operator, "tool": "scripts/uber_activate_store.py", **extra},
        ))
        await db.commit()


async def _activate(connection: DeliveryConnection, code: str, redirect_uri: str) -> None:
    store_id = connection.external_store_id
    async with httpx.AsyncClient() as http:
        token = await provisioning.exchange_authorization_code(http, SANDBOX, code=code, redirect_uri=redirect_uri)
        print("Merchant token received (not shown).")
        visible = await provisioning.list_merchant_store_ids(http, token, environment=SANDBOX)
        print(f"Stores this merchant login can see: {len(visible)}")
        if store_id not in visible:
            raise DeliveryProviderError("UBER_EATS", "the mapped store is not among this merchant's stores")
        await provisioning.activate_store(
            http, token, environment=SANDBOX, store_id=store_id, integrator_store_id=str(connection.business_id),
        )
        del token
        print(f"Activate Integration: accepted by Uber for store {_mask(store_id)} (is_order_manager=true).")

        redis = await get_redis()
        app_token = await auth.get_operational_token(redis, http, SANDBOX)
        try:
            status = await client.get_store_status(
                http, app_token, store_id, api_base_url=get_environment_config(SANDBOX).api_base_url,
            )
            print(f"Store status with the app's own token: HTTP 200, status={status.get('status')!r}")
        except (DeliveryProviderError, DeliveryProviderTimeout) as exc:
            print(f"Store status with the app's own token still fails: HTTP {getattr(exc, 'status_code', None)} "
                  "(activation can take a moment to propagate; re-check before testing orders)")
        del app_token


async def _run(args: argparse.Namespace) -> None:
    connection = await _load_connection(args.business_id)
    print(f"Connection: business {args.business_id}, store {_mask(connection.external_store_id)}, sandbox/SANDBOX")

    redirect = urlparse(args.redirect_uri)
    port = args.listen_port or redirect.port or 80
    state = secrets.token_urlsafe(32)
    listener = RedirectListener(port, redirect.path or "/", state)  # bound before the browser opens
    url = provisioning.build_authorize_url(SANDBOX, redirect_uri=args.redirect_uri, state=state)
    print(f"Listening on 127.0.0.1:{port}{redirect.path} for Uber's redirect (up to 10 minutes).")
    print(url if args.show_url else "Opening Uber's consent page in your browser (link not shown; --show-url prints it).")
    print("Sign in there — in a private window — with the MERCHANT login for this store, then approve.")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    params = await asyncio.to_thread(listener.wait)
    if not params:
        await _audit(connection, args.operator, "failed", reason="no redirect within 10 minutes")
        sys.exit("No redirect from Uber within 10 minutes; nothing was changed.")
    if "error" in params:
        await _audit(connection, args.operator, "failed", reason=f"consent declined: {params['error'][:64]}")
        sys.exit(f"Uber declined the consent: error={params['error'][:64]!r}; nothing was changed.")
    if not params.get("code"):
        await _audit(connection, args.operator, "failed", reason="redirect carried no code")
        sys.exit("REFUSED: the redirect carried no code; nothing was changed.")

    try:
        await _activate(connection, params["code"], args.redirect_uri)
    except (DeliveryProviderError, DeliveryProviderTimeout) as exc:
        status = getattr(exc, "status_code", None)
        await _audit(connection, args.operator, "failed", reason=exc.__class__.__name__, http_status=status)
        sys.exit(f"FAILED: {exc}" if status is None else f"FAILED: {exc.__class__.__name__}, HTTP {status}.")
    await _audit(connection, args.operator, "succeeded", is_order_manager=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--business-id", required=True, type=uuid.UUID)
    parser.add_argument("--operator", required=True, help="who is running this (recorded in the audit trail)")
    parser.add_argument("--redirect-uri", default="http://localhost:8766/uber/oauth/callback")
    parser.add_argument("--listen-port", type=int, default=None)
    parser.add_argument("--show-url", action="store_true", help="print the full consent link (it contains the client id)")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
