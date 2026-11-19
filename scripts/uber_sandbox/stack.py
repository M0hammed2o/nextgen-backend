"""Isolated local Uber Eats SANDBOX stack — env handling shared by every step.

Own Postgres (127.0.0.1:55797) + Redis (127.0.0.1:55798) containers, own random
secrets (sandbox.env, next to this file, outside the repo, never printed),
ENVIRONMENT=staging so the production boot guard applies, sandbox-only Uber.
"""
import base64
import os
import secrets
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "sandbox.env")
TENANTS = os.path.join(HERE, "tenants.json")
LOGINS = os.path.join(HERE, "logins.local.txt")
PG, REDIS = "nextgen-uber-sandbox-postgres", "nextgen-uber-sandbox-redis"
PG_PORT, REDIS_PORT = 55797, 55798
PRODUCTION_UBER = ("UBER_EATS_CLIENT_ID", "UBER_EATS_CLIENT_SECRET")


def _rand(n: int = 48) -> str:
    return secrets.token_urlsafe(n)


def create() -> None:
    if os.path.exists(ENV_FILE):
        sys.exit(f"{ENV_FILE} already exists; refusing to regenerate secrets for a running stack.")
    pg_password = _rand(24)
    values = {
        "ENVIRONMENT": "staging",
        "DEBUG": "false",
        "DATABASE_URL": f"postgresql+asyncpg://nextgen_sandbox:{pg_password}@127.0.0.1:{PG_PORT}/nextgen_uber_sandbox",
        "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "JWT_SECRET_KEY": _rand(),
        "JWT_ADMIN_SECRET_KEY": _rand(),
        "CREDENTIALS_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "META_APP_SECRET": _rand(),
        "META_VERIFY_TOKEN": _rand(),
        "WHATSAPP_DEFAULT_ACCESS_TOKEN": "sandbox-no-whatsapp-" + _rand(),
        "BILLING_ENABLED": "false",
        "UBER_EATS_WEBHOOK_ENABLED": "true",
        "UBER_EATS_SANDBOX_ONLY_DEPLOYMENT": "true",
    }
    subprocess.run(["docker", "run", "-d", "--name", PG, "-e", "POSTGRES_USER=nextgen_sandbox",
                    "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=nextgen_uber_sandbox",
                    "-p", f"127.0.0.1:{PG_PORT}:5432", "postgres:16-alpine"],
                   check=True, env={**os.environ, "POSTGRES_PASSWORD": pg_password}, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "run", "-d", "--name", REDIS, "-p", f"127.0.0.1:{REDIS_PORT}:6379", "redis:7-alpine"],
                   check=True, stdout=subprocess.DEVNULL)
    with open(ENV_FILE, "w", encoding="utf-8") as fh:
        fh.write("# Local Uber sandbox stack. Random, sandbox-only values. Never commit, never print.\n")
        for k, v in values.items():
            fh.write(f"{k}={v}\n")
    print(f"Created containers {PG} (127.0.0.1:{PG_PORT}) and {REDIS} (127.0.0.1:{REDIS_PORT}); wrote {ENV_FILE}")


def _read(path: str) -> dict:
    out = {}
    for raw in open(path, encoding="utf-8-sig"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def load(uber_creds_file: str = ".env.uber-sandbox") -> None:
    """Put the sandbox stack's env into this process. Refuses anything production-shaped."""
    if os.path.exists(".env.backend"):
        sys.exit("REFUSED: .env.backend exists in the working directory; Settings would load it.")
    env = _read(ENV_FILE)
    creds = _read(uber_creds_file)
    for k in PRODUCTION_UBER:
        if k in creds or k in env:
            sys.exit(f"REFUSED: production variable {k} present.")
    for k in ("UBER_EATS_SANDBOX_CLIENT_ID", "UBER_EATS_SANDBOX_CLIENT_SECRET"):
        if not creds.get(k):
            sys.exit(f"MISSING {k} in {uber_creds_file}")
        env[k] = creds[k]
    # Dashboard "Basic HMAC" signing keys (both required for this sandbox).
    for k in ("UBER_EATS_SANDBOX_WEBHOOK_SIGNING_KEY", "UBER_EATS_SANDBOX_WEBHOOK_SECONDARY_SIGNING_KEY"):
        if not creds.get(k):
            sys.exit(f"MISSING {k} in {uber_creds_file}")
        env[k] = creds[k]
    if not env["DATABASE_URL"].startswith("postgresql+asyncpg://nextgen_sandbox:") or f":{PG_PORT}/" not in env["DATABASE_URL"]:
        sys.exit("REFUSED: DATABASE_URL is not the sandbox container.")
    for k in PRODUCTION_UBER:
        os.environ.pop(k, None)
    os.environ.update(env)


if __name__ == "__main__" and sys.argv[1:] == ["create"]:
    create()
