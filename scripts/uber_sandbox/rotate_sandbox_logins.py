"""Sandbox-only. Fix the seeded owners' .invalid emails (refused by EmailStr on /auth/login) and rotate the
exposed owner passwords and staff PINs through the app's own endpoints. Writes the new values ONLY to
logins.local.txt; prints statuses, never a credential."""
import os, re, secrets, subprocess, sys, httpx
SB = os.path.dirname(os.path.abspath(__file__)); BASE = "http://127.0.0.1:8780/v1"
PSQL = ["docker", "exec", "nextgen-uber-sandbox-postgres", "psql", "-U", "nextgen_sandbox", "-d", "nextgen_uber_sandbox", "-tAc"]
def sql(q): return subprocess.run(PSQL + [q], capture_output=True, text=True, check=True).stdout.strip()
lines, out = open(f"{SB}/logins.local.txt").read().splitlines(), []
c = httpx.Client(timeout=30)
for line in lines:
    m = re.match(r"(.+?): business code (\S+) \| owner (\S+) / (\S+) \| staff PIN (\d+)", line)
    if not m: out.append(line); continue
    name, code, old_email, old_pw, _ = m.groups(); n = name.rsplit(" ", 1)[-1]
    email = f"owner+uber-sandbox-test-{n}@example.com"   # RFC 2606 reserved: valid syntax, never deliverable
    if old_email != email:
        bid = sql(f"select business_id from business_users where email='{old_email}' and role='OWNER'")
        assert bid, f"{name}: owner row not found"
        sql(f"update business_users set email='{email}' where email='{old_email}' and role='OWNER' and business_id='{bid}'")
    new_pw = secrets.token_urlsafe(18)
    r = c.post(f"{BASE}/auth/set-password", json={"email": email, "current_password": old_pw, "new_password": new_pw})
    print(f"{name}: set-password HTTP {r.status_code}"); r.raise_for_status()
    r = c.post(f"{BASE}/auth/login", json={"email": email, "password": new_pw})
    print(f"{name}: login with new password HTTP {r.status_code}"); r.raise_for_status()
    H = {"Authorization": "Bearer " + r.json()["tokens"]["access_token"]}
    r = c.post(f"{BASE}/auth/login", json={"email": email, "password": old_pw})
    print(f"{name}: login with OLD password HTTP {r.status_code} (expect 401)")
    staff_id = sql(f"select s.id from business_users s join business_users o on o.business_id=s.business_id where o.email='{email}' and s.role='STAFF'")
    r = c.post(f"{BASE}/business/staff/{staff_id}/pin/rotate", headers=H)
    print(f"{name}: staff PIN rotate HTTP {r.status_code}"); r.raise_for_status()
    out.append(f"{name}: business code {code} | owner {email} / {new_pw} | staff PIN {r.json()['pin']}")
open(f"{SB}/logins.local.txt", "w").write("\n".join(out) + "\n")
print("logins.local.txt updated (values not shown)")
