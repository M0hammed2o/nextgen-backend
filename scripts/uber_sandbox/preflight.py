"""Read-only preflight before a real Uber sandbox test order (local sandbox only). Run from the backend worktree root.

Checks, per test restaurant: Uber's own view (store status, pos_data order manager, menu has Best Burger),
our connection row, the Best Burger mapping, recipe + current patty stock, and that the staff kitchen/delivery
endpoints answer through the normal owner login. Never writes, never prints a credential, token or key.
"""
import asyncio
import hmac
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SB)
sys.path.insert(0, os.getcwd())
import stack  # noqa: E402

stack.load()
import asyncpg  # noqa: E402
import httpx  # noqa: E402

API = "http://127.0.0.1:8780/v1"
TENANTS = {"TR1": "c7c244dd-d4f0-49e5-85fc-52b8dd16575c", "TR2": "b8a3161a-3789-475d-b303-6b9467e87d4e"}
STORES = {"TR1": "c8d01022-14a3-4bad-829a-6a2e7c2f5217", "TR2": "bd6f7d16-a20c-4758-8985-293ffd0f50de"}
DSN = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")


def walk_titles(node, out):
    if isinstance(node, dict):
        t = node.get("title")
        if isinstance(t, dict) and "translations" in t:
            out.append((node.get("id"), " ".join(t["translations"].values())))
        for v in node.values():
            walk_titles(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_titles(v, out)


async def main():
    from backend.app.db.session import get_redis
    from backend.app.delivery.uber import auth
    from backend.app.delivery.uber.environments import SANDBOX, get_environment_config

    env = get_environment_config(SANDBOX)
    db = await asyncpg.connect(DSN)
    http = httpx.AsyncClient(timeout=30)
    token = await auth.get_operational_token(await get_redis(), http, SANDBOX)
    H = {"Authorization": f"Bearer {token}"}
    staff = httpx.AsyncClient(base_url=API, timeout=30)
    for tr, bid in TENANTS.items():
        sid = STORES[tr]
        print(f"\n{tr} (store …{sid[-4:]}, tenant …{bid[-4:]})")
        # Uber's side, read-only
        st = await http.get(f"{env.api_base_url}/v1/eats/store/{sid}/status", headers=H)
        status = st.json().get("status") if st.status_code == 200 else None
        check("Uber store status ONLINE", status == "ONLINE", f"HTTP {st.status_code} status={status!r}")
        pd = await http.get(f"{env.api_base_url}/v1/eats/stores/{sid}/pos_data", headers=H)
        j = pd.json() if pd.status_code == 200 else {}
        ours = hmac.compare_digest(str(j.get("order_manager_client_id") or ""), env.client_id)
        check("Uber integration enabled, NextGen app is order manager", pd.status_code == 200 and j.get("integration_enabled") and ours,
              f"HTTP {pd.status_code} integration_enabled={j.get('integration_enabled')} order_manager_is_us={ours}")
        wh = j.get("webhooks_config") or {}
        # orders.notification is not in webhooks_config: these flags are only the OPTIONAL webhook types
        print(f"  INFO  optional webhook types (not needed for an ASAP order): {dict(_flatten(wh))}")
        mn = await http.get(f"{env.api_base_url}/v2/eats/stores/{sid}/menus", headers=H)
        titles = []
        walk_titles(mn.json() if mn.status_code == 200 else {}, titles)
        burger_ids = [i for i, t in titles if "Best Burger" in t and i]
        check("Uber menu has Best Burger", mn.status_code == 200 and burger_ids, f"HTTP {mn.status_code}; item ids …{[b[-6:] for b in burger_ids]}")
        # Our side
        conn = await db.fetchrow("select status, environment, store_status, auto_accept_enabled, external_store_id from delivery_connections "
                                 "where business_id = $1 and provider = 'UBER_EATS'", bid)
        check("NextGen connection SANDBOX (order-taking), sandbox, this store", conn and conn["status"] == "SANDBOX" and conn["environment"] == "sandbox"
              and conn["external_store_id"] == sid, f"{dict(conn) if conn else None}" .replace(sid, "…" + sid[-4:]))
        other = await db.fetchval("select count(*) from delivery_connections where external_store_id = $1 and business_id <> $2", sid, bid)
        check("store id belongs to no other tenant", other == 0, f"{other} other connection(s)")
        maps = await db.fetch("select coalesce(m.external_id, m.external_key) as external_key, m.is_ignored, mi.name, mi.business_id, mi.is_active and not mi.is_deleted as is_available from external_product_mappings m "
                              "left join menu_items mi on mi.id = m.menu_item_id where m.business_id = $1 and m.platform = 'UBER_EATS' "
                              "and m.kind = 'ITEM'", bid)
        bm = [m for m in maps if m["external_key"] in burger_ids]
        check("Best Burger mapped to this tenant's own menu item", bm and all(m["name"] == "Best Burger" and str(m["business_id"]) == bid
              and not m["is_ignored"] for m in bm), f"{len(maps)} item mapping(s); burger -> {[(m['name'], m['is_available']) for m in bm]}")
        rec = await db.fetchrow("select ing.id, ing.name, ri.quantity_milli from menu_items mi join recipes r on r.menu_item_id = mi.id "
                                "join recipe_items ri on ri.recipe_id = r.id join ingredients ing on ing.id = ri.ingredient_id "
                                "where mi.business_id = $1 and mi.name = 'Best Burger'", bid)
        check("Best Burger recipe uses Burger patty", rec is not None, f"{rec['name']} x{rec['quantity_milli'] / 1000:g}" if rec else "no recipe")
        if rec:
            lvl = await db.fetchval("select coalesce(sum(quantity_milli), 0) from stock_movements where business_id = $1 and ingredient_id = $2", bid, rec["id"])
            n = await db.fetchval("select count(*) from stock_movements where business_id = $1 and ingredient_id = $2", bid, rec["id"])
            print(f"  INFO  starting Burger patty stock = {float(lvl) / 1000:g} ({n} movement(s))")
        # Kitchen/staff endpoints through the normal owner login
        line = next(x for x in open(os.path.join(SB, "logins.local.txt")) if x.startswith(f"NextGen Intelligence Test Restaurant {tr[-1]}:"))
        email, pw = re.search(r"owner (\S+) / (\S+)", line).groups()
        r = await staff.post("/auth/login", json={"email": email, "password": pw})
        ok = r.status_code == 200 and r.json()["user"]["business_id"] == bid
        check("owner login lands in this tenant", ok, f"HTTP {r.status_code}")
        if ok:
            h = {"Authorization": "Bearer " + r.json()["tokens"]["access_token"]}
            k = await staff.get("/business/orders", params={"status": "NEW,ACCEPTED,IN_PROGRESS,READY"}, headers=h)
            check("kitchen order list answers (KDS feed)", k.status_code == 200, f"HTTP {k.status_code}")
            q = await staff.get("/business/delivery/queue", headers=h)
            check("delivery queue answers", q.status_code == 200, f"HTTP {q.status_code}")
    stale = await db.fetchval("select count(*) from delivery_orders where status in ('RECEIVED','FETCHING','READY_TO_ACCEPT','ACCEPT_UNCONFIRMED')")
    print()
    check("no stuck in-flight delivery orders", stale == 0, f"{stale}")
    print(f"\n{sum(results)}/{len(results)} preflight checks passed")


def _flatten(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flatten(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


asyncio.run(main())
