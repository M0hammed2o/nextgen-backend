#!/usr/bin/env python3
"""
NextGen local print bridge — thermal receipt printing for the browser POS.

    Browser POS  --(HTTPS)-->  https://staff.nextgen...
         |
         +--(HTTP, loopback only)-->  127.0.0.1:9107  -->  thermal printer

WHY THIS EXISTS
---------------
A browser cannot open a raw TCP socket or write to a USB device, so ESC/POS
(and therefore a cash-drawer kick, or routing a kitchen slip to a second
printer) is impossible from the page alone. This bridge is the smallest
component that closes that gap.

IT IS NOT ALWAYS REQUIRED. The recommended first path for Ciao Pizza is
Chrome's --kiosk-printing against the Windows driver, which needs no
software at all. Install this only when you need the cash drawer, a separate
kitchen printer, or byte-exact control. See README.md.

DEPENDENCIES: none. Standard library only, so deployment on a till PC is
"install Python, copy two files" rather than a package environment.

SECURITY
--------
* Binds 127.0.0.1 ONLY. Never 0.0.0.0 — this service prints and opens a cash
  drawer, and must not be reachable from the restaurant's network or its
  guest wifi.
* Requires a per-device bearer token, so another page open in the same
  browser cannot silently trigger prints or pop the drawer.
* Echoes CORS only for explicitly allow-listed origins, and answers Chrome's
  Private Network Access preflight so an HTTPS page may reach loopback.
* Refuses to start with a default/blank token.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
DEFAULT_PORT = 9107
LOOPBACK = "127.0.0.1"
MAX_BODY_BYTES = 2 * 1024 * 1024  # a receipt is a few KB; this is generous

log = logging.getLogger("nextgen-print-bridge")


# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "token": "",
    "port": DEFAULT_PORT,
    "allowed_origins": [
        "http://localhost:5173",
        "http://localhost:8080",
    ],
    "printers": {
        "CUSTOMER_RECEIPT": {
            # windows | tcp | file
            "transport": "windows",
            "windows_printer_name": "",
            "host": "",
            "port": 9100,
            "file_path": "",
            "capabilities": {
                "supports_cut": True,
                "supports_drawer": True,
                "supports_qr": True,
                "codepage": "cp437",
            },
        }
    },
}


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, indent=2)
        raise SystemExit(
            f"Wrote a starter config to {path}.\n"
            "Set a strong 'token' and your printer details, then start again.\n"
            "Generate a token with:  python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)

    token = (config.get("token") or "").strip()
    if len(token) < 16:
        raise SystemExit(
            "Refusing to start: 'token' must be at least 16 characters.\n"
            "This service can print and open a cash drawer; it must not be "
            "reachable without a secret."
        )
    return config


# ── Transports ───────────────────────────────────────────────────────────────

def send_windows(payload: bytes, printer_name: str) -> str:
    """
    Write raw bytes to a Windows print queue.

    Prefers the pywin32 RAW datatype, which bypasses the driver's rendering
    so ESC/POS reaches the printer untouched. Falls back to copying the file
    to the share, which works on a surprising number of till setups.
    """
    try:
        import win32print  # type: ignore

        handle = win32print.OpenPrinter(printer_name)
        try:
            job = win32print.StartDocPrinter(handle, 1, ("NextGen receipt", None, "RAW"))
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, payload)
            win32print.EndPagePrinter(handle)
            win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)
        return f"windows/raw job {job}"
    except ImportError:
        pass  # fall through

    with tempfile.NamedTemporaryFile(delete=False, suffix=".prn") as tmp:
        tmp.write(payload)
        temp_path = tmp.name
    try:
        result = subprocess.run(
            ["cmd", "/c", "copy", "/b", temp_path, f"\\\\localhost\\{printer_name}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "copy to printer share failed")
        return "windows/share-copy"
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def send_tcp(payload: bytes, host: str, port: int) -> str:
    """Raw 9100 printing — the standard for a LAN/Ethernet thermal printer."""
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(payload)
    return f"tcp/{host}:{port}"


def send_file(payload: bytes, path: str) -> str:
    """
    Append to a file — a USB printer exposed as a device node, or a capture
    file for diagnosing what was actually sent.
    """
    with open(path, "ab") as handle:
        handle.write(payload)
    return f"file/{path}"


def dispatch(payload: bytes, printer: dict) -> str:
    transport = (printer.get("transport") or "windows").lower()
    if transport == "windows":
        name = printer.get("windows_printer_name") or ""
        if not name:
            raise RuntimeError("windows_printer_name is not configured")
        return send_windows(payload, name)
    if transport == "tcp":
        host = printer.get("host") or ""
        if not host:
            raise RuntimeError("host is not configured for the tcp transport")
        return send_tcp(payload, host, int(printer.get("port") or 9100))
    if transport == "file":
        path = printer.get("file_path") or ""
        if not path:
            raise RuntimeError("file_path is not configured")
        return send_file(payload, path)
    raise RuntimeError(f"Unknown transport {transport!r}")


# ── ESC/POS the bridge itself can emit ───────────────────────────────────────

DRAWER_KICK = b"\x1b\x70\x00\x32\xfa"  # pin 2, 50 ms on / 250 ms off


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = f"NextGenPrintBridge/{VERSION}"
    config: dict = {}

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------
    def _origin_allowed(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin and origin in (self.config.get("allowed_origins") or []):
            return origin
        return None

    def _cors(self) -> None:
        origin = self._origin_allowed()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
            # Chrome's Private Network Access: an HTTPS page reaching loopback
            # sends a preflight expecting this header, and silently fails
            # without it.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization") or ""
        expected = f"Bearer {self.config.get('token')}"
        # Length-independent comparison is unnecessary here (loopback, local
        # attacker already has the machine) but costs nothing.
        import hmac

        return hmac.compare_digest(header, expected)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            # Deliberately unauthenticated and free of secrets: the PWA uses
            # it to decide whether to offer bridge printing at all.
            self._json(200, {
                "status": "ok",
                "service": "nextgen-print-bridge",
                "version": VERSION,
                "profiles": sorted((self.config.get("printers") or {}).keys()),
            })
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0]
        if route not in ("/print", "/drawer"):
            self._json(404, {"error": "not_found"})
            return

        if not self._authorised():
            self._json(401, {"error": "unauthorized", "message": "Bad or missing bearer token"})
            return

        try:
            body = self._read_body()
        except ValueError as exc:
            self._json(413, {"error": "bad_request", "message": str(exc)})
            return
        except json.JSONDecodeError:
            self._json(400, {"error": "bad_request", "message": "Body must be JSON"})
            return

        profile = body.get("profile") or "CUSTOMER_RECEIPT"
        printer = (self.config.get("printers") or {}).get(profile)
        if not printer:
            self._json(404, {
                "error": "unknown_profile",
                "message": f"No printer configured for profile {profile!r}",
                "profiles": sorted((self.config.get("printers") or {}).keys()),
            })
            return

        if route == "/drawer":
            payload = DRAWER_KICK
        else:
            encoded = body.get("data_base64")
            if not encoded:
                self._json(400, {"error": "bad_request", "message": "data_base64 is required"})
                return
            try:
                payload = base64.b64decode(encoded, validate=True)
            except Exception:
                self._json(400, {"error": "bad_request", "message": "data_base64 is not valid base64"})
                return
            copies = max(1, min(int(body.get("copies") or 1), 5))
            payload = payload * copies

        try:
            detail = dispatch(payload, printer)
        except Exception as exc:
            # A print failure must be reported clearly and NEVER retried
            # automatically — the sale is already saved server-side, and the
            # cashier decides whether to reprint.
            log.error("print failed on profile %s: %s", profile, exc)
            self._json(502, {
                "error": "print_failed",
                "message": str(exc),
                "profile": profile,
                "hint": "The sale is saved. Fix the printer and use Reprint.",
            })
            return

        self._json(200, {"status": "printed", "profile": profile, "transport": detail,
                         "bytes": len(payload)})


def main() -> int:
    parser = argparse.ArgumentParser(description="NextGen local thermal print bridge")
    parser.add_argument("--config", default="bridge-config.json")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)
    port = args.port or int(config.get("port") or DEFAULT_PORT)

    Handler.config = config
    server = ThreadingHTTPServer((LOOPBACK, port), Handler)

    log.info("NextGen print bridge %s", VERSION)
    log.info("Listening on http://%s:%s  (loopback only)", LOOPBACK, port)
    log.info("Profiles: %s", ", ".join(sorted((config.get("printers") or {}).keys())))
    log.info("Allowed origins: %s", ", ".join(config.get("allowed_origins") or []) or "(none)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
