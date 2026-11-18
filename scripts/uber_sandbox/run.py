"""Run one sandbox process with the stack's env loaded. From the backend worktree root:

    python <this> migrate          # alembic upgrade head on the sandbox DB
    python <this> backend 8780     # uvicorn, 127.0.0.1 only, trusts X-Forwarded-For from the local tunnel
    python <this> worker           # ARQ worker (backend.app.worker.WorkerSettings)
    python <this> script <path> [args...]   # any repo script (e.g. scripts/uber_activate_store.py)
"""
import os
import runpy
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack  # noqa: E402

stack.load()
cmd = sys.argv[1]
if cmd == "migrate":
    sys.exit(subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=os.environ).returncode)
if cmd == "backend":
    port = int(sys.argv[2])
    print(f"Sandbox backend on 127.0.0.1:{port} — ENVIRONMENT={os.environ['ENVIRONMENT']}, sandbox-only Uber", flush=True)
    sys.exit(subprocess.call([sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
                              "--port", str(port), "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1",
                              "--log-level", "warning"]))
if cmd == "worker":
    print("Sandbox ARQ worker starting", flush=True)
    sys.exit(subprocess.call([sys.executable, "-m", "arq", "backend.app.worker.WorkerSettings"]))
if cmd == "script":
    sys.path.insert(0, os.getcwd())
    sys.argv = sys.argv[2:]
    runpy.run_path(sys.argv[0], run_name="__main__")
    sys.exit(0)
sys.exit(f"unknown command {cmd!r}")
