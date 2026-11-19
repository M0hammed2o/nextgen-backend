"""Sandbox-only: log in as TR1's local owner and run Uber catalogue import + list mappings. Never prints secrets."""
import json, re, sys, httpx, os
SB = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8780/v1"
# Log in through the normal /auth/login (owner emails fixed 2026-11-08). Credentials come from the local
# logins file and are never printed; neither is the token.
BID = os.environ.get("BID", "c7c244dd-d4f0-49e5-85fc-52b8dd16575c")
N = {"c7c244dd-d4f0-49e5-85fc-52b8dd16575c": "1", "b8a3161a-3789-475d-b303-6b9467e87d4e": "2"}[BID]
line = [l for l in open(f"{SB}/logins.local.txt") if l.startswith(f"NextGen Intelligence Test Restaurant {N}:")][0]
email, pw = re.search(r"owner (\S+) / (\S+)", line).groups()
c = httpx.Client(timeout=60)
r = c.post(f"{BASE}/auth/login", json={"email": email, "password": pw}); r.raise_for_status()
assert r.json()["user"]["business_id"] == BID
H = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
print("acting as OWNER, business", BID)
def show(label, resp):
    print(f"{label}: HTTP {resp.status_code}"); 
    try: print(json.dumps(resp.json(), indent=1)[:4000])
    except Exception: print(resp.text[:500])
cmd = sys.argv[1]
if cmd == "import":
    show("import", c.post(f"{BASE}/business/delivery/menu-mappings/import", json={"provider": "UBER_EATS"}, headers=H))
    show("mappings", c.get(f"{BASE}/business/delivery/menu-mappings", headers=H))
elif cmd == "get":
    show(sys.argv[2], c.get(f"{BASE}{sys.argv[2]}", headers=H))
elif cmd == "post":
    show(sys.argv[2], c.post(f"{BASE}{sys.argv[2]}", json=json.loads(sys.argv[3]), headers=H))
