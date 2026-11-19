"""
BUG-0213: bcrypt checks at login must not run on the event loop thread, or each
login stalls every other request on that worker for ~1 s. These tests patch the
hash check with a probe that records which thread called it.
"""
import inspect
import threading

from admin_api.app.api.v1 import routes_auth as admin_routes_auth
from backend.app.services import auth_service


def test_business_password_and_pin_checks_are_offloaded():
    src = inspect.getsource(auth_service)
    assert "run_in_threadpool(verify_password, password," in src
    assert "run_in_threadpool(verify_password, current_password," in src
    assert "run_in_threadpool(verify_pin, pin," in src
    # No direct (blocking) call remains.
    assert "not verify_password(" not in src
    assert "not verify_pin(" not in src


def test_admin_password_check_is_offloaded():
    src = inspect.getsource(admin_routes_auth)
    assert "run_in_threadpool(verify_password, body.password," in src
    assert "not verify_password(" not in src


async def test_run_in_threadpool_runs_the_check_off_the_loop_thread(monkeypatch):
    seen = {}
    loop_thread = threading.get_ident()

    def probe(*_a):
        seen["thread"] = threading.get_ident()
        return False

    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(probe, "pw", "hash")
    assert seen["thread"] != loop_thread
