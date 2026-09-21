"""
BUG-0067, second half — a genuinely unhandled exception (e.g. the PDF
renderer raising `FPDFUnicodeEncodingException`, or any other bug) must
still come back to the browser as a normal JSON error response WITH CORS
headers, not a raw 500 that makes the browser report a misleading
"blocked by CORS policy" error.

Root cause: registering `@app.exception_handler(Exception)` ALONE is not
enough. Starlette special-cases the literal `Exception`/500 key — it routes
that handler to `ServerErrorMiddleware`, which sits OUTSIDE every
`add_middleware()` layer including CORS (`app.add_middleware()` makes the
LAST-added middleware outermost — real production order, confirmed via
traceback during this fix, is CORSMiddleware -> UnhandledExceptionMiddleware
-> CorrelationIDMiddleware -> RequestLoggingMiddleware -> ExceptionMiddleware
-> router). A response built by ServerErrorMiddleware never passes back
through CORSMiddleware's header injection. The actual fix is
`UnhandledExceptionMiddleware` (backend/app/core/errors.py), a real ASGI
middleware registered BEFORE CORSMiddleware so it sits inside it.

This test builds a FastAPI app with the SAME middleware classes, in the SAME
order, as the real apps (see backend/app/main.py / admin_api/app/main.py)
and a route that raises an unmocked exception — standing in for a genuinely
broken PDF-generation path — to prove the fix holds at the actual ASGI-stack
level, not just at the exception-handler-function level.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend.app.core.errors import UnhandledExceptionMiddleware, register_error_handlers
from backend.app.core.middleware import CorrelationIDMiddleware, RequestLoggingMiddleware

ALLOWED_ORIGIN = "http://localhost:5173"


def _build_test_app() -> FastAPI:
    app = FastAPI()

    # Same order as backend/app/main.py and admin_api/app/main.py.
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIDMiddleware)
    app.add_middleware(UnhandledExceptionMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    @app.get("/boom/unicode")
    async def boom_unicode():
        # Stands in for build_commercial_invoice_pdf raising
        # FPDFUnicodeEncodingException on an un-sanitised character that
        # somehow still reaches it (defense-in-depth: the route itself no
        # longer needs a try/except for this to behave correctly).
        from fpdf.errors import FPDFUnicodeEncodingException

        raise FPDFUnicodeEncodingException(
            text_index=13, character="—", font_name="helvetica"
        )

    @app.get("/boom/generic")
    async def boom_generic():
        raise RuntimeError("some other genuinely broken code path")

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    return app


def _build_app_without_middleware_fix() -> FastAPI:
    """
    Same as `_build_test_app()` but WITHOUT `UnhandledExceptionMiddleware` —
    reproduces the exact pre-fix production configuration (only the
    `@app.exception_handler(Exception)` registered by
    `register_error_handlers`, no catch-all middleware). Used to prove the
    CORS gap is real and specifically caused by the middleware ordering
    described above, not by a bug in the JSON-shaping logic.
    """
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(CorrelationIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    @app.get("/boom/generic")
    async def boom_generic():
        raise RuntimeError("some other genuinely broken code path")

    return app


client = TestClient(_build_test_app(), raise_server_exceptions=False)
_client_without_fix = TestClient(_build_app_without_middleware_fix(), raise_server_exceptions=False)


class TestUnhandledExceptionGetsStructuredResponse:
    def test_unicode_pdf_style_exception_returns_500_json_not_raw_crash(self):
        resp = client.get("/boom/unicode", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        body = resp.json()
        assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
        assert "error" in body

    def test_generic_unhandled_exception_returns_500_json(self):
        resp = client.get("/boom/generic", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"


class TestUnhandledExceptionResponseCarriesCorsHeaders:
    """
    The actual bug: without a registered catch-all handler, this response
    would come back with NO Access-Control-Allow-Origin header, and the
    browser would report a CORS failure instead of the real 500.
    """

    def test_cors_header_present_on_unicode_exception_response(self):
        resp = client.get("/boom/unicode", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    def test_cors_header_present_on_generic_exception_response(self):
        resp = client.get("/boom/generic", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    def test_cors_header_present_on_success_response_unchanged(self):
        """Baseline: the fix must not alter CORS behaviour for the happy path."""
        resp = client.get("/ok", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN

    def test_disallowed_origin_still_gets_no_cors_header_on_exception(self):
        """
        The fix must not weaken CORS enforcement — an origin NOT on the
        allowlist must still get no Access-Control-Allow-Origin, even on the
        error path.
        """
        resp = client.get("/boom/generic", headers={"Origin": "http://evil.example"})

        assert resp.status_code == 500
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers.keys()}


class TestExceptionHandlerAloneIsNotSufficientRegressionProof:
    """
    Locks in the actual root cause: an `@app.exception_handler(Exception)`
    with no catch-all middleware reproduces BUG-0067's CORS-masking symptom.
    If this test ever starts failing (i.e. the header IS present), it means
    Starlette's behaviour changed upstream, not that this codebase's fix is
    wrong — in that case `UnhandledExceptionMiddleware` becomes redundant
    but still harmless, so don't remove it without re-verifying this test.
    """

    def test_exception_handler_alone_reproduces_the_missing_cors_header(self):
        resp = _client_without_fix.get("/boom/generic", headers={"Origin": ALLOWED_ORIGIN})

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers.keys()}
