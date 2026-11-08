"""
Uber sandbox order monitor (local sandbox only). Run from the backend worktree root.

  python order_watch.py watch [--drive]     one line per step as it happens (for the Monitor tool)
  python order_watch.py trace <uber order id | delivery order id>     PASS/FAIL report of the whole chain

--drive acts as each test restaurant's staff through the normal /auth/login and the same endpoints the staff
app uses: accept (READY_TO_ACCEPT), then the kitchen ticket IN_PROGRESS -> READY (pushes "ready" to Uber). It
stops there: Uber has no restaurant-side "collected" call, its courier picks the order up. Never prints a credential, token, signature or key.
"""
import asyncio
import glob
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SB)
sys.path.insert(0, os.getcwd())
import stack  # noqa: E402

stack.load()
import asyncpg  # noqa: E402
import httpx  # noqa: E402

API = "http://127.0.0.1:8780/v1"
TENANTS = {"c7c244dd-d4f0-49e5-85fc-52b8dd16575c": "TR1", "b8a3161a-3789-475d-b303-6b9467e87d4e": "TR2"}
STORES = {"c8d01022-14a3-4bad-829a-6a2e7c2f5217": "TR1", "bd6f7d16-a20c-4758-8985-293ffd0f50de": "TR2"}
DSN = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
LOG = os.path.join(SB, "backend.log")
LOG_MARKERS = re.compile(r"DELIVERY_|UBER_WEBHOOK|SIGNED_WITH_CLIENT_SECRET|ERROR|Traceback")


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Staff:
    """One owner session per test tenant via the normal login (rate-limited: log in once, re-login on 401)."""

    def __init__(self):
        self.tokens: dict[str, str] = {}
        self.http = httpx.AsyncClient(base_url=API, timeout=30)

    async def _login(self, bid: str) -> str:
        n = TENANTS[bid][-1]
        line = next(x for x in open(os.path.join(SB, "logins.local.txt")) if x.startswith(f"NextGen Intelligence Test Restaurant {n}:"))
        email, pw = re.search(r"owner (\S+) / (\S+)", line).groups()
        r = await self.http.post("/auth/login", json={"email": email, "password": pw})
        r.raise_for_status()
        assert r.json()["user"]["business_id"] == bid, "login landed in the wrong tenant"
        self.tokens[bid] = r.json()["tokens"]["access_token"]
        return self.tokens[bid]

    async def call(self, bid: str, method: str, path: str, **kw) -> httpx.Response:
        for _ in range(2):
            token = self.tokens.get(bid) or await self._login(bid)
            r = await self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
            if r.status_code != 401:
                return r
            self.tokens.pop(bid, None)
        return r


async def watch(drive: bool) -> None:
    db = await asyncpg.connect(DSN)
    staff = Staff() if drive else None
    seen_caps = set(glob.glob(os.path.join(SB, "captures", "*.json")))
    last_event = await db.fetchval("select coalesce(max(received_at), now()) from delivery_webhook_events")
    dstate: dict = {r["id"]: r["status"] for r in await db.fetch("select id, status from delivery_orders")}
    kstate: dict = {r["id"]: r["status"] for r in await db.fetch("select id, status from orders where business_id = any($1::uuid[])", list(TENANTS))}
    last_move = await db.fetchval("select coalesce(max(created_at), now()) from stock_movements")
    log_pos = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    driven: set = set()
    say(f"watching (drive={'on' if drive else 'off'}) — delivery orders so far: {len(dstate)}")
    while True:
        for cap in sorted(set(glob.glob(os.path.join(SB, "captures", "*.json"))) - seen_caps):
            seen_caps.add(cap)
            m = json.load(open(cap))
            say(f"EDGE   webhook at gateway: status={m.get('status')} ua={(m.get('user-agent') or '-')[:60]!r} "
                f"x-environment={m.get('x-environment')} sig={'yes' if m.get('x-uber-signature') else 'no'} "
                f"sig-new={'yes' if m.get('x-uber-signature-new') else 'no'} capture={os.path.basename(cap)[:-5]}")
        for r in await db.fetch("select received_at, event_type, external_store_id, business_id, signature_valid, error_category, event_id "
                                "from delivery_webhook_events where received_at > $1 order by received_at", last_event):
            last_event = r["received_at"]
            tenant = TENANTS.get(str(r["business_id"]), "no tenant")
            store = STORES.get(r["external_store_id"] or "", r["external_store_id"] or "-")
            say(f"INTAKE {r['event_type'] or '-'} store={store} -> {tenant} signature_valid={r['signature_valid']} result={r['error_category']}")
        for r in await db.fetch("select id, business_id, external_order_id, display_id, status, accept_deadline_at, failure_reason, "
                                "unresolved_items_json, order_id from delivery_orders order by created_at"):
            if dstate.get(r["id"]) != r["status"]:
                dstate[r["id"]] = r["status"]
                tenant = TENANTS.get(str(r["business_id"]), str(r["business_id"]))
                extra = f" failure={r['failure_reason'][:120]!r}" if r["failure_reason"] else ""
                extra += f" unresolved={json.dumps(r['unresolved_items_json'])[:120]}" if r["unresolved_items_json"] else ""
                say(f"ORDER  {tenant} uber={r['external_order_id']} #{r['display_id']} -> {r['status']} "
                    f"deadline={r['accept_deadline_at']:%H:%M:%S}{extra}" if r["accept_deadline_at"] else
                    f"ORDER  {tenant} uber={r['external_order_id']} -> {r['status']}{extra}")
                if drive and r["status"] == "READY_TO_ACCEPT" and r["id"] not in driven:
                    driven.add(r["id"])
                    resp = await staff.call(str(r["business_id"]), "POST", f"/business/delivery/orders/{r['id']}/accept")
                    say(f"DRIVE  staff accept -> HTTP {resp.status_code} {resp.text[:160] if resp.status_code >= 400 else ''}")
        for r in await db.fetch("select o.id, o.business_id, o.status, o.order_number, o.external_order_id from orders o "
                                "where o.business_id = any($1::uuid[]) order by o.created_at", list(TENANTS)):
            if kstate.get(r["id"]) != r["status"]:
                new = r["id"] not in kstate
                kstate[r["id"]] = r["status"]
                tenant = TENANTS[str(r["business_id"])]
                say(f"KDS    {tenant} {'NEW ticket' if new else 'ticket'} #{r['order_number']} uber={r['external_order_id']} -> {r['status']}")
                if drive and r["external_order_id"]:
                    # Stops at READY: Uber has no restaurant-side "collected" call (its courier
                    # picks up), so COLLECTED is never set on Uber's behalf.
                    nxt = {"ACCEPTED": "IN_PROGRESS", "IN_PROGRESS": "READY"}.get(r["status"])
                    if nxt:
                        await asyncio.sleep(3)
                        resp = await staff.call(str(r["business_id"]), "POST", f"/business/orders/{r['id']}/status", json={"status": nxt})
                        say(f"DRIVE  kitchen {r['status']} -> {nxt}: HTTP {resp.status_code} {resp.text[:160] if resp.status_code >= 400 else ''}")
        for r in await db.fetch("select created_at, business_id, quantity_milli, movement_type, source_type, source_id from stock_movements "
                                "where created_at > $1 order by created_at", last_move):
            last_move = r["created_at"]
            say(f"STOCK  {TENANTS.get(str(r['business_id']), '?')} {r['movement_type']} {r['quantity_milli'] / 1000:+g} source={r['source_type']}:{r['source_id']}")
        if os.path.exists(LOG) and os.path.getsize(LOG) > log_pos:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                f.seek(log_pos)
                for line in f:
                    if LOG_MARKERS.search(line) and "DELIVERY_WEBHOOK_ENTRY" not in line:
                        say("LOG    " + line.strip()[:230])
                log_pos = f.tell()
        await asyncio.sleep(2)


async def trace(ref: str) -> None:
    db = await asyncpg.connect(DSN)
    row = await db.fetchrow("select * from delivery_orders where external_order_id = $1 or id::text = $1", ref)
    if row is None:
        sys.exit(f"no delivery order for {ref!r}")
    ok_all = []

    def step(name, ok, detail=""):
        ok_all.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    tenant = TENANTS.get(str(row["business_id"]), str(row["business_id"]))
    store = STORES.get(row["external_store_id"])
    ev = await db.fetch("select signature_valid, error_category, event_type, business_id from delivery_webhook_events "
                        "where raw_payload_json::text like '%' || $1 || '%' order by received_at", row["external_order_id"])
    caps = [json.load(open(p)) for p in glob.glob(os.path.join(SB, "captures", "*.json"))
            if row["external_order_id"] in open(p[:-5] + ".body", encoding="utf-8", errors="ignore").read()]
    step("1 Uber -> public webhook (gateway capture)", caps, f"{len(caps)} delivery(ies); ua={caps[0].get('user-agent') if caps else '-'!r}")
    step("2 signature verified", any(e["signature_valid"] and e["error_category"] == "OK" for e in ev),
         f"{sum(1 for e in ev if e['signature_valid'])} verified event(s); categories={sorted({e['error_category'] for e in ev})}")
    step("3 correct restaurant", store == tenant and all(TENANTS.get(str(e["business_id"])) == tenant for e in ev if e["signature_valid"]),
         f"store {store} -> tenant {tenant}")
    n_rows = await db.fetchval("select count(*) from delivery_orders where external_order_id = $1", row["external_order_id"])
    step("4 order created once", n_rows == 1, f"{n_rows} delivery_orders row(s); status={row['status']}; display #{row['display_id']}")
    kitchen = await db.fetchrow("select * from orders where id = $1", row["order_id"]) if row["order_id"] else None
    items = await db.fetch("select quantity, name_snapshot as name, line_total_cents from order_items where order_id = $1", row["order_id"]) if kitchen else []
    step("5 kitchen ticket", kitchen is not None and kitchen["business_id"] == row["business_id"],
         f"#{kitchen['order_number']} status={kitchen['status']} items={[(i['quantity'], i['name']) for i in items]}" if kitchen else "no orders row")
    step("6 accepted (NextGen)", row["accepted_at"] is not None and row["status"] in ("ACCEPTED", "FULFILLED"),
         f"accepted_at={row['accepted_at']} status={row['status']}")
    try:
        from backend.app.db.session import get_redis
        from backend.app.delivery.uber import auth, client
        from backend.app.delivery.uber.environments import SANDBOX, get_environment_config
        env = get_environment_config(SANDBOX)
        async with httpx.AsyncClient() as h:
            token = await auth.get_operational_token(await get_redis(), h, SANDBOX)
            uber = await client.get_order_details(h, token, row["external_order_id"], api_base_url=env.api_base_url)
        state = uber.get("current_state") or uber.get("state") or uber.get("status")
        step("7 Uber's own view of the order (GET /v2/eats/order, read-only)", state and str(state).upper() not in ("CREATED", "OFFERED", "DENIED", "CANCELED", "CANCELLED"),
             f"current_state={state!r}")
    except Exception as exc:  # report, never crash the trace
        step("7 Uber's own view of the order (GET /v2/eats/order, read-only)", False, f"{exc.__class__.__name__}: {getattr(exc, 'status_code', '')}")
    moves = await db.fetch("select quantity_milli, idempotency_key from stock_movements where source_type = 'ORDER' and source_id = $1",
                           row["order_id"]) if row["order_id"] else []
    expected = sum(i["quantity"] for i in items)  # 1 patty per Best Burger in the sandbox recipe
    got = -sum(m["quantity_milli"] for m in moves) / 1000
    step("8 stock deducted exactly once", (got == expected) if kitchen else not moves,
         f"{len(moves)} movement(s), {got:g} patty(ies) for {expected} burger(s); keys unique={len({m['idempotency_key'] for m in moves}) == len(moves)}")
    print(f"\n{sum(ok_all)}/{len(ok_all)} steps passed for {tenant} uber order {row['external_order_id']}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["watch"]:
        asyncio.run(watch("--drive" in sys.argv))
    elif sys.argv[1:2] == ["trace"] and len(sys.argv) > 2:
        asyncio.run(trace(sys.argv[2]))
    else:
        sys.exit(__doc__)
