"""Smokin Joes -- Cowey Road (business code GREJ5A) configuration utility.

Populates the EXISTING production business GREJ5A -- menu, ingredients,
recipes, add-ons, removable ingredients, loyalty program/rewards -- using
only the platform's existing, already-deployed HTTP APIs. Creates no new
business, writes no orders/customers/payments, never touches any other
tenant.

SAFETY
------
- Dry-run by DEFAULT. Nothing is written unless you pass --execute.
- Verifies the authenticated tenant's business_code == GREJ5A (via
  GET /v1/business/settings) BEFORE touching anything else, in both
  dry-run and --execute mode. Any mismatch aborts immediately.
- Idempotent: menu/ingredients/recipes reuse the platform's own
  update-by-name bulk import (never duplicates on a second run);
  add-ons/removable-ingredients/loyalty rewards are each looked up by
  name first and only created if genuinely missing; attaching an add-on
  to an item uses the platform's own idempotent attach endpoint.
- Credentials are NEVER hardcoded, logged, or written to the report.
  Reads NEXTGEN_ACCESS_TOKEN (preferred -- obtain this yourself via your
  own login, outside this script) or NEXTGEN_OWNER_EMAIL +
  NEXTGEN_OWNER_PASSWORD from the environment only.
- Imports menu/ingredients/recipes ONLY -- never orders, customers,
  payments, or any historical/financial data.

USAGE
-----
    export NEXTGEN_API_BASE_URL="https://api.nextgenintelligence.co.za"
    export NEXTGEN_ACCESS_TOKEN="<a real owner/manager access token you already have>"
    # -- or, instead of a token --
    export NEXTGEN_OWNER_EMAIL="owner@...>"
    export NEXTGEN_OWNER_PASSWORD="<...>"

    python scripts/configure_smokin_joes_grej5a.py                 # dry run (default, safe)
    python scripts/configure_smokin_joes_grej5a.py --execute       # actually writes

    # Optional: point at a different local copy of the import files
    python scripts/configure_smokin_joes_grej5a.py --files-dir "C:\\path\\to\\Smokin-Joes-Cowey-Road-Demo"
"""
import argparse
import json
import os
import sys
import uuid
import urllib.error
import urllib.request

EXPECTED_BUSINESS_CODE = "GREJ5A"  # the target tenant -- not a secret, just an identifier

DEFAULT_FILES_DIR = r"C:\Users\junsm\Downloads\Smokin-Joes-Cowey-Road-Demo"
MENU_CSV = "1_menu_import_cowey-road.csv"
INGREDIENTS_CSV = "2_ingredients_import_DEMO.csv"
RECIPES_CSV = "3_recipes_import_cowey-road.csv"

ADD_ONS = [
    {"name": "Extra Cheese", "price_cents": 1200},
    {"name": "Extra Beef Patty", "price_cents": 3500},
]
ADD_ON_TARGETS = ["Smoking Joe Burger", "The Gypsy King Burger"]

REMOVABLE_INGREDIENTS = {
    "Smoking Joe Burger": [
        {"id": "rm-sauce", "name": "Sauce"},
        {"id": "rm-cheese", "name": "Cheese"},
        {"id": "rm-jalapenos", "name": "Jalapenos"},
    ],
    "The Gypsy King Burger": [
        {"id": "rm-cheese", "name": "Cheese"},
    ],
}

LOYALTY_PROGRAM = {
    "is_enabled": True, "points_per_rand": 1, "points_per_order": 10,
    "points_expiry_days": None, "min_redemption_points": 50,
}
LOYALTY_REWARDS = [
    {"name": "DEMO -- Free Regular Fries", "points_cost": 150, "value_cents": 4600},
    {"name": "DEMO -- Free Milkshake", "points_cost": 350, "value_cents": 9400},
    {"name": "DEMO -- R50 Off Order", "points_cost": 250, "value_cents": 5000},
    {"name": "DEMO -- R100 Off Order", "points_cost": 500, "value_cents": 10000},
]


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:300]}")


class Client:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token  # never printed, never logged

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        if extra:
            h.update(extra)
        return h

    def get(self, path):
        req = urllib.request.Request(self.base_url + path, headers=self._headers(), method="GET")
        return self._send(req)

    def post_json(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        return self._send(req)

    def put_json(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base_url + path, data=data, method="PUT",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        return self._send(req)

    def post_file(self, path, file_path):
        boundary = uuid.uuid4().hex
        with open(file_path, "rb") as f:
            content = f.read()
        filename = os.path.basename(file_path)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        return self._send(req)

    @staticmethod
    def _send(req):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode(errors="replace"))


def get_token():
    token = os.environ.get("NEXTGEN_ACCESS_TOKEN")
    if token:
        return token
    email = os.environ.get("NEXTGEN_OWNER_EMAIL")
    password = os.environ.get("NEXTGEN_OWNER_PASSWORD")
    if not (email and password):
        print(
            "ERROR: set NEXTGEN_ACCESS_TOKEN, or NEXTGEN_OWNER_EMAIL + NEXTGEN_OWNER_PASSWORD, "
            "in the environment. Never pass these as command-line arguments (they'd be visible "
            "in shell history/process listings).",
            file=sys.stderr,
        )
        sys.exit(2)
    base_url = os.environ.get("NEXTGEN_API_BASE_URL", "").rstrip("/")
    client = Client(base_url, token=None)
    req = urllib.request.Request(
        base_url + "/v1/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: login failed ({e.code}). Check NEXTGEN_OWNER_EMAIL/NEXTGEN_OWNER_PASSWORD.", file=sys.stderr)
        sys.exit(2)
    return resp["tokens"]["access_token"]


class Report:
    def __init__(self):
        self.created, self.updated, self.skipped, self.failed = [], [], [], []

    def add(self, bucket, item, detail=""):
        getattr(self, bucket).append(f"{item}" + (f" -- {detail}" if detail else ""))

    def print_summary(self):
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for label, items in (("CREATED", self.created), ("UPDATED", self.updated),
                              ("SKIPPED (already present)", self.skipped), ("FAILED", self.failed)):
            print(f"\n{label} ({len(items)}):")
            for it in items:
                print(f"  - {it}")


def verify_tenant(client: Client) -> dict:
    settings = client.get("/v1/business/settings")
    actual = settings.get("business_code")
    if actual != EXPECTED_BUSINESS_CODE:
        print(
            f"ABORT: authenticated tenant's business_code is {actual!r}, expected "
            f"{EXPECTED_BUSINESS_CODE!r}. Refusing to touch anything.",
            file=sys.stderr,
        )
        sys.exit(3)
    print(f"Tenant verified: business_code={actual}, name={settings.get('name')!r}, id={settings.get('id')}")
    return settings


def _count(value) -> int:
    """Preview/commit responses mix plain counts and lists-of-names across
    the three import endpoints (menu returns counts; ingredients/recipes
    return lists) -- normalise either shape to a count for display."""
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    return int(value)


def import_sheet(client: Client, report: Report, sheet: str, file_path: str, execute: bool):
    print(f"\n--- {sheet} import ({os.path.basename(file_path)}) ---")
    preview = client.post_file(f"/v1/business/import/{sheet}/preview", file_path)
    total = preview.get("total_rows")
    errors = preview.get("error_count", 0)
    would_create = _count(preview.get("would_create"))
    already_exists = _count(preview.get("would_skip_existing")) or _count(preview.get("would_update"))
    print(f"  preview: total_rows={total} errors={errors} new={would_create} already_present={already_exists}")
    if errors:
        for e in preview.get("errors", [])[:20]:
            print(f"    ROW {e['row']}: {e['message']}")
        report.add("failed", f"{sheet} import", f"{errors} row error(s) -- see output above, nothing committed")
        return
    if not execute:
        report.add("skipped", f"{sheet} import", f"DRY RUN -- would add {would_create} new, {already_exists} already present")
        return
    result = client.post_file(f"/v1/business/import/{sheet}/commit", file_path)
    created = _count(result.get("created")) or _count(result.get("would_create"))
    skipped_existing = result.get("skipped_existing") or []
    summary_parts = [f"{k}={_count(v)}" for k, v in result.items() if isinstance(v, (int, list))]
    summary = ", ".join(summary_parts) or "nothing needed changing"
    print(f"  committed: {summary}")
    bucket = "created" if created else ("skipped" if skipped_existing or not any(_count(v) for v in result.values() if isinstance(v, (int, list))) else "updated")
    report.add(bucket, f"{sheet} import", summary)


def configure_add_ons(client: Client, report: Report, execute: bool):
    print("\n--- Add-ons ---")
    existing = {a["name"]: a for a in client.get("/v1/business/menu/add-ons")}
    created_ids = {}
    for spec in ADD_ONS:
        if spec["name"] in existing:
            print(f"  '{spec['name']}' already exists -- skip create")
            report.add("skipped", f"add-on {spec['name']!r}", "already exists")
            created_ids[spec["name"]] = existing[spec["name"]]["id"]
            continue
        if not execute:
            print(f"  DRY RUN: would create add-on {spec['name']!r} (R{spec['price_cents']/100:.2f})")
            report.add("skipped", f"add-on {spec['name']!r}", "DRY RUN -- would create")
            continue
        try:
            created = client.post_json("/v1/business/menu/add-ons", spec)
            created_ids[spec["name"]] = created["id"]
            print(f"  created add-on {spec['name']!r} id={created['id']}")
            report.add("created", f"add-on {spec['name']!r}")
        except ApiError as e:
            report.add("failed", f"add-on {spec['name']!r}", str(e))

    if not execute:
        return created_ids

    items = {i["name"]: i for i in client.get("/v1/business/menu/items")}
    for target_name in ADD_ON_TARGETS:
        item = items.get(target_name)
        if not item:
            report.add("failed", f"attach add-ons to {target_name!r}", "menu item not found -- import menu first")
            continue
        for add_on_name, add_on_id in created_ids.items():
            try:
                client.post_json(f"/v1/business/menu/items/{item['id']}/add-ons/{add_on_id}", {})
                report.add("updated", f"attach {add_on_name!r} -> {target_name!r}", "idempotent attach")
            except ApiError as e:
                report.add("failed", f"attach {add_on_name!r} -> {target_name!r}", str(e))
    return created_ids


def configure_removable_ingredients(client: Client, report: Report, execute: bool):
    print("\n--- Removable ingredients ---")
    if not execute:
        for item_name, removables in REMOVABLE_INGREDIENTS.items():
            names = ", ".join(r["name"] for r in removables)
            print(f"  DRY RUN: would ensure {item_name!r} allows removing: {names}")
            report.add("skipped", f"removable ingredients on {item_name!r}", "DRY RUN")
        return

    items = {i["name"]: i for i in client.get("/v1/business/menu/items")}
    for item_name, removables in REMOVABLE_INGREDIENTS.items():
        item = items.get(item_name)
        if not item:
            report.add("failed", f"removable ingredients on {item_name!r}", "menu item not found -- import menu first")
            continue
        current_options = item.get("options_json") or {}
        current_removable = current_options.get("removable_ingredients") or []
        current_ids = {r["id"] for r in current_removable}
        missing = [r for r in removables if r["id"] not in current_ids]
        if not missing:
            report.add("skipped", f"removable ingredients on {item_name!r}", "already configured")
            continue
        new_options = dict(current_options)
        new_options["removable_ingredients"] = current_removable + missing
        try:
            client.put_json(f"/v1/business/menu/items/{item['id']}", {"options_json": new_options})
            added = ", ".join(r["name"] for r in missing)
            report.add("updated", f"removable ingredients on {item_name!r}", f"added: {added}")
        except ApiError as e:
            report.add("failed", f"removable ingredients on {item_name!r}", str(e))


def configure_loyalty(client: Client, report: Report, execute: bool):
    print("\n--- Loyalty ---")
    try:
        program = client.get("/v1/business/loyalty/program")
    except ApiError as e:
        if e.status == 409:
            report.add("failed", "loyalty program", "LOYALTY_NOT_ENABLED -- needs a super admin to enable the entitlement first")
            return
        raise

    needs_update = any(program.get(k) != v for k, v in LOYALTY_PROGRAM.items())
    if needs_update:
        if execute:
            client.put_json("/v1/business/loyalty/program", LOYALTY_PROGRAM)
            report.add("updated", "loyalty program", "earn/redemption rules applied")
        else:
            report.add("skipped", "loyalty program", "DRY RUN -- would update earn/redemption rules")
    else:
        report.add("skipped", "loyalty program", "already configured correctly")

    existing_rewards = {r["name"] for r in client.get("/v1/business/loyalty/rewards")}
    for reward in LOYALTY_REWARDS:
        if reward["name"] in existing_rewards:
            report.add("skipped", f"reward {reward['name']!r}", "already exists")
            continue
        if not execute:
            report.add("skipped", f"reward {reward['name']!r}", "DRY RUN -- would create")
            continue
        try:
            client.post_json("/v1/business/loyalty/rewards", reward)
            report.add("created", f"reward {reward['name']!r}")
        except ApiError as e:
            report.add("failed", f"reward {reward['name']!r}", str(e))


def verify_pos_visibility(client: Client, report: Report):
    print("\n--- POS visibility check ---")
    try:
        items = client.get("/v1/business/menu/items")
        active = [i for i in items if i.get("is_active")]
        print(f"  {len(active)}/{len(items)} menu items are active (POS only shows active items)")
        report.add("skipped" if len(active) == 21 else "failed",
                    "POS visibility", f"{len(active)} active items (expected 21)")
    except ApiError as e:
        report.add("failed", "POS visibility check", str(e))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually write. Default is dry-run.")
    parser.add_argument("--files-dir", default=DEFAULT_FILES_DIR)
    args = parser.parse_args()

    base_url = os.environ.get("NEXTGEN_API_BASE_URL")
    if not base_url:
        print("ERROR: set NEXTGEN_API_BASE_URL in the environment.", file=sys.stderr)
        sys.exit(2)
    token = get_token()
    client = Client(base_url, token)

    print(f"Mode: {'EXECUTE (will write)' if args.execute else 'DRY RUN (no writes)'}")
    verify_tenant(client)  # aborts on any mismatch, before anything else

    report = Report()
    import_sheet(client, report, "menu", os.path.join(args.files_dir, MENU_CSV), args.execute)
    import_sheet(client, report, "ingredients", os.path.join(args.files_dir, INGREDIENTS_CSV), args.execute)
    import_sheet(client, report, "recipes", os.path.join(args.files_dir, RECIPES_CSV), args.execute)
    configure_add_ons(client, report, args.execute)
    configure_removable_ingredients(client, report, args.execute)
    configure_loyalty(client, report, args.execute)
    if args.execute:
        verify_pos_visibility(client, report)

    report.print_summary()
    if not args.execute:
        print("\nThis was a DRY RUN -- nothing was written. Re-run with --execute to apply.")


if __name__ == "__main__":
    main()
