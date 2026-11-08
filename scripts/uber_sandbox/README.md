# Local Uber Eats sandbox: operator tooling

Sandbox only. Production, Render, WhatsApp and real restaurants are never involved. These scripts run the isolated local stack (its own Postgres and Redis containers, own random secrets, sandbox-only Uber app) that the two Uber test stores are integrated with. See `docs/MARKETPLACE_INTEGRATIONS.md` §4 for the why.

Everything secret stays next to the scripts and is git-ignored (`.gitignore` here): `sandbox.env`, `logins.local.txt`, `tenants.json`, `captures/`, logs. Nothing here prints a password, token, key or signature.

Run each command from the backend worktree root. `SB` is this folder, or a copy of it outside the repo, which is how the live sandbox runs.

| Script | What it does |
|---|---|
| `stack.py` | Shared environment loader. It creates the containers and `sandbox.env` (random secrets) on first use and refuses production Uber credentials. |
| `run.py migrate\|backend 8780\|worker\|script <path>` | Runs one process with the sandbox environment loaded. |
| `seed.py` | Creates the two test tenants and their SANDBOX Uber connections, idempotently. Owner emails are `@example.com`. |
| `gateway.py 8781` | The public edge. It forwards only the Uber webhook, `/health` and the OAuth callback; everything else is 404. It logs each webhook's header names, its User-Agent and the HTTP result, and saves the raw body plus its per-message signature headers to `captures/` (capped). |
| `lt_watchdog.sh` | **Stable public URL**: `https://nextgen-uber-sbx-q7k3.loca.lt` via localtunnel. It always re-requests the same subdomain, restarts on exit or after 3 failed health checks, and logs an ALERT if the subdomain isn't granted. |
| `tunnel_watchdog.sh` | Fallback: a Cloudflare quick tunnel (`--protocol http2 --edge-ip-version 4`). Its URL changes on every restart and is written to `current_tunnel_url.txt`. |
| `public_webhook_proof.py <public url>` | Sends HMAC-signed store events (real sandbox keys, computed in-process) through the public URL, and reads every verdict from the database: tenant routing, the rotation header, replay, a bad signature, a tampered body, a client-secret signature. Store events queue no job and never call Uber. |
| `order_watch.py watch [--drive]` | Live monitor: one line per step (edge → intake → delivery order → kitchen ticket → stock → logs). `--drive` acts as restaurant staff through the normal login: accept, then IN_PROGRESS → READY (pushes "ready" to Uber) → COLLECTED. |
| `order_watch.py trace <uber order id>` | PASS/FAIL report for one order: capture, signature, tenant, created once, kitchen ticket, accepted, Uber's own state (read-only `GET /v2/eats/order`), stock deducted exactly once. |
| `owner_api.py import\|get\|post` | Calls the manager API as a test tenant's owner through the normal login. `BID=<business id>` selects the tenant. |
| `setup_inventory.py` | Gives each tenant's "Best Burger" a recipe (1 × Burger patty) and 20 in opening stock, idempotently. |
| `rotate_sandbox_logins.py` | Rotates the owner passwords (`/auth/set-password`) and staff PINs (`/business/staff/{id}/pin/rotate`). The new values go to `logins.local.txt` only. |
| `nobrowser.py` | Wrapper that stops `scripts/uber_activate_store.py` opening the default browser, so the consent page can be opened in a private window instead. |

## When a test order is placed

```
python "$SB/order_watch.py" watch --drive      # start BEFORE the order; Uber allows 11.5 min to accept
python "$SB/order_watch.py" trace <uber order id>
```

## Known limits

- The backend runs on this workstation. While it sleeps, nothing is processed. Uber retries on 5xx and timeouts, up to 7 sends; an order not accepted within 11.5 minutes is cancelled by Uber.
- localtunnel is a free best-effort service. It shows a "Tunnel website ahead" page (HTTP 511) to browser-like User-Agents. Uber's User-Agent is undocumented; the gateway logs it on the first real event. While this machine is offline, someone else could claim the subdomain and receive signed sandbox webhooks. They could not forge one, but it means this isn't fit for production.
