"""Public edge of the local Uber sandbox: the ONLY thing the HTTPS tunnel reaches.

Forwards exactly:
  POST /v1/webhook/delivery/UBER_EATS  -> sandbox backend (127.0.0.1:8780), raw body + headers intact
  GET  /health                         -> sandbox backend
  GET  /uber/oauth/callback            -> activation tool's one-shot listener (127.0.0.1:8766)
Everything else is 404, so the backend's other routes (incl. the Meta webhook,
whose signature check is skipped when ENVIRONMENT=development) are never public.

Run: python <this> 8781
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

BACKEND = "http://127.0.0.1:8780"
CALLBACK = "http://127.0.0.1:8766"
_FORWARD_HEADERS = ("content-type", "x-uber-signature", "x-uber-signature-new", "x-environment", "user-agent")
client = httpx.AsyncClient(timeout=30)


CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return request.headers.get("cf-connecting-ip") or forwarded or (request.client.host if request.client else "unknown")


def _capture(request: Request, body: bytes, status: int) -> str:
    """Sandbox diagnostics: keep the exact raw body and the per-message signature headers (HMAC outputs,
    not keys) of every webhook, so a genuine Uber event can be inspected and replayed byte for byte."""
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    if len(body) > 256 * 1024 or len(os.listdir(CAPTURE_DIR)) > 4000:   # bounded: the URL is public
        return "-"
    name =f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:6]}"
    with open(os.path.join(CAPTURE_DIR, name + ".body"), "wb") as f:
        f.write(body)
    meta = {k: request.headers.get(k) for k in ("x-uber-signature", "x-uber-signature-new", "x-environment",
                                                "user-agent", "content-type")}
    meta.update(client_ip=_client_ip(request), status=status, host=request.headers.get("host"))
    with open(os.path.join(CAPTURE_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return name


async def _forward(request: Request, target: str) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_HEADERS}
    headers["x-forwarded-for"] = _client_ip(request)
    upstream = await client.request(request.method, target, content=await request.body(), headers=headers)
    passthrough = {k: v for k, v in upstream.headers.items() if k.lower() == "content-type"}
    return Response(upstream.content, status_code=upstream.status_code, headers=passthrough)


async def uber_webhook(request: Request) -> Response:
    # Header NAMES only (never values), plus the Authorization scheme word, so the
    # first real Uber event shows which authentication the dashboard's "Basic" sends.
    names = sorted({k.lower() for k in request.headers.keys()} - {"cf-ray", "cf-visitor", "cdn-loop", "cf-ipcountry"})
    auth = request.headers.get("authorization", "")
    scheme = auth.split(" ", 1)[0][:16] if auth else "-"
    body = await request.body()
    response = await _forward(request, f"{BACKEND}/v1/webhook/delivery/UBER_EATS")
    capture = _capture(request, body, response.status_code)
    ua = request.headers.get("user-agent", "-")[:80]
    print(f"{datetime.now(timezone.utc).isoformat()} WEBHOOK from={_client_ip(request)} bytes={len(body)} "
          f"auth_scheme={scheme} ua={ua!r} capture={capture} headers={names} -> {response.status_code}", flush=True)
    return response


async def health(request: Request) -> Response:
    return await _forward(request, f"{BACKEND}/health")


async def oauth_callback(request: Request) -> Response:
    try:
        return await _forward(request, f"{CALLBACK}/uber/oauth/callback?{request.url.query}")
    except httpx.HTTPError:
        return Response("No activation is waiting for this callback.", status_code=404)


async def not_found(request: Request) -> Response:
    return Response(status_code=404)


app = Starlette(routes=[
    Route("/v1/webhook/delivery/UBER_EATS", uber_webhook, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
    Route("/uber/oauth/callback", oauth_callback, methods=["GET"]),
    Route("/{path:path}", not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
