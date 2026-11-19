"""Sandbox-only, idempotent: give each test tenant's mapped 'Best Burger' a recipe (1 x Burger patty) and opening
stock, through the normal owner login and the app's own inventory/recipe endpoints, so a real Uber order shows
WHEN stock is deducted and that it happens once. Prints statuses/quantities only."""
import os, re, sys, httpx
SB = os.path.dirname(os.path.abspath(__file__)); BASE = "http://127.0.0.1:8780/v1"
TENANTS = {"1": "c7c244dd-d4f0-49e5-85fc-52b8dd16575c", "2": "b8a3161a-3789-475d-b303-6b9467e87d4e"}
for n, bid in TENANTS.items():
    line = [l for l in open(f"{SB}/logins.local.txt") if l.startswith(f"NextGen Intelligence Test Restaurant {n}:")][0]
    email, pw = re.search(r"owner (\S+) / (\S+)", line).groups()
    c = httpx.Client(timeout=30, base_url=BASE)
    r = c.post("/auth/login", json={"email": email, "password": pw}); r.raise_for_status()
    assert r.json()["user"]["business_id"] == bid
    c.headers["Authorization"] = "Bearer " + r.json()["tokens"]["access_token"]
    item = next(i for i in c.get("/business/menu/items").json() if i["name"] == "Best Burger")
    ing = next((i for i in c.get("/business/inventory/ingredients").json() if i["name"] == "Burger patty"), None)
    if ing is None:
        r = c.post("/business/inventory/ingredients", json={"name": "Burger patty", "canonical_unit": "UNIT", "unit_label": "patty"})
        print(f"TR{n} ingredient create HTTP {r.status_code}"); r.raise_for_status(); ing = r.json()
    moves = c.get(f"/business/inventory/stock/{ing['id']}/movements").json()
    if not (moves.get("movements") if isinstance(moves, dict) else moves):
        r = c.post("/business/inventory/opening-stock", json={"ingredient_id": ing["id"], "quantity": "20", "unit": "EACH",
                                                               "source": "Uber sandbox test stock"})
        print(f"TR{n} opening stock HTTP {r.status_code} {r.text[:160] if r.status_code >= 400 else ''}"); r.raise_for_status()
    r = c.put("/business/recipes", json={"menu_item_id": item["id"], "items": [{"ingredient_id": ing["id"], "quantity": "1", "unit": "EACH"}]})
    print(f"TR{n} recipe upsert HTTP {r.status_code} {r.text[:160] if r.status_code >= 400 else ''}"); r.raise_for_status()
    lvl = next((s for s in c.get("/business/inventory/stock").json() if s.get("ingredient_id") == ing["id"]), {})
    print(f"TR{n} stock now: { {k: v for k, v in lvl.items() if 'quant' in k or 'on_hand' in k or 'unit' in k} }")
