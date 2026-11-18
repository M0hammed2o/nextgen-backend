"""Sandbox proof over the PUBLIC endpoint: HMAC-signed (real sandbox keys, computed in-process, never printed)
store events per tenant, rotation header, replay, bad signature, tampered body, client-secret signature.
Every verdict is read from the application database, never inferred from the HTTP 200. Store events queue no
job and never call Uber. Usage: python <this> <public base url>"""
import hashlib, hmac, json, os, subprocess, sys, time, uuid
SB = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, SB)
import stack; stack.load()
import httpx
BASE = sys.argv[1].rstrip("/"); URL = f"{BASE}/v1/webhook/delivery/UBER_EATS"
K1 = os.environ["UBER_EATS_SANDBOX_WEBHOOK_SIGNING_KEY"]; K2 = os.environ["UBER_EATS_SANDBOX_WEBHOOK_SECONDARY_SIGNING_KEY"]
CS = os.environ["UBER_EATS_SANDBOX_CLIENT_SECRET"]
TR = {"TR1": ("c8d01022-14a3-4bad-829a-6a2e7c2f5217", "c7c244dd-d4f0-49e5-85fc-52b8dd16575c"),
      "TR2": ("bd6f7d16-a20c-4758-8985-293ffd0f50de", "b8a3161a-3789-475d-b303-6b9467e87d4e")}
PSQL = ["docker", "exec", "nextgen-uber-sandbox-postgres", "psql", "-U", "nextgen_sandbox", "-d", "nextgen_uber_sandbox", "-tAc"]
sql = lambda q: subprocess.run(PSQL + [q], capture_output=True, text=True, check=True).stdout.strip()
sig = lambda k, b: hmac.new(k.encode(), b, hashlib.sha256).hexdigest()
c = httpx.Client(timeout=30, headers={"User-Agent": "nextgen-sandbox-proof/1.0", "Content-Type": "application/json"})
def body(store):  # store.provisioned shape as observed from Uber on 2026-11-07
    mid = str(uuid.uuid4())
    b = json.dumps({"event_type": "store.provisioned", "store_id": store, "partner_store_id": "proof",
                    "webhook_meta": {"client_id": "redacted", "webhook_config_id": "proof", "webhook_msg_timestamp": int(time.time()),
                                     "webhook_msg_uuid": mid}, "resource_href": "", "perform_refresh_menu": False},
                   separators=(",", ":")).encode()
    return b, mid
def post(b, headers): r = c.post(URL, content=b, headers=headers); time.sleep(1.5); return r.status_code
def rows(mid): return sql(f"select coalesce(business_id::text,'-')||'|'||signature_valid||'|'||coalesce(error_category,'-') "
                          f"from delivery_webhook_events where event_id like '%{mid}' order by received_at")
orders0, events0 = sql("select count(*) from delivery_orders"), sql("select count(*) from delivery_webhook_events")
results = []
def check(name, ok, detail): results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
b1, m1 = body(TR["TR1"][0]); s = post(b1, {"X-Uber-Signature": sig(K1, b1)})
check("TR1 signed (primary key)", rows(m1) == f"{TR['TR1'][1]}|true|OK", f"HTTP {s}, db={rows(m1)}")
b2, m2 = body(TR["TR2"][0]); s = post(b2, {"X-Uber-Signature": "0" * 64, "X-Uber-Signature-New": sig(K2, b2)})
check("TR2 signed (secondary key, rotation header)", rows(m2) == f"{TR['TR2'][1]}|true|OK", f"HTTP {s}, db={rows(m2)}")
s = post(b1, {"X-Uber-Signature": sig(K1, b1)})
check("replay of TR1 bytes+signature", rows(m1) == f"{TR['TR1'][1]}|true|OK", f"HTTP {s}, rows for that id={len(rows(m1).splitlines())} (still 1)")
b3, m3 = body(TR["TR1"][0]); s = post(b3, {"X-Uber-Signature": sig("wrong-key-" + "x" * 40, b3)})
check("invalid signature", rows(m3) == "", f"HTTP {s}; nothing stored under the attacker's id; the refusal is an unverified row with no tenant")
mt = str(uuid.uuid4()); tampered = b1.replace(TR["TR1"][0].encode(), TR["TR2"][0].encode()).replace(m1.encode(), mt.encode())
s = post(tampered, {"X-Uber-Signature": sig(K1, b1)})
check("TR1 signature on a body re-pointed at TR2", rows(mt) == "", f"HTTP {s}; nothing stored under the tampered id, no TR2 row")
b4, m4 = body(TR["TR1"][0]); s = post(b4, {"X-Uber-Signature": sig(CS, b4)})
check("client-secret signature (sandbox has signing keys)", rows(m4) == "", f"HTTP {s}; refused")
unverified = sql("select count(*) from delivery_webhook_events where business_id is null and signature_valid = false and received_at > now() - interval '2 minutes'")
check("refusals logged without a tenant", int(unverified) >= 3, f"{unverified} unverified tenant-less rows in the last 2 min")
check("no delivery order created", sql("select count(*) from delivery_orders") == orders0, f"delivery_orders still {orders0}")
print(f"\n{sum(results)}/{len(results)} checks passed")
