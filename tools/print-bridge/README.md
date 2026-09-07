# NextGen thermal printing — install runbook

Two ways to print a receipt to a conventional thermal till printer. **Try
Path A first.** It needs no software installed and covers the Ciao Pizza
pilot's stated requirement (automatic customer receipt on their existing
printer). Path B exists for the cash drawer and a separate kitchen printer.

---

## Path A — Chrome kiosk printing (recommended, zero code)

Virtually every thermal receipt printer (Epson TM-T20/T82/T88, Star
TSP100/143, and the common generic 58 mm clones) ships a Windows driver and
appears as an ordinary printer. Chrome can print to it silently.

### 1. Install the printer
Install the manufacturer's Windows driver. Confirm it prints a Windows test
page. USB and Ethernet both work — once installed, Windows presents them
identically.

### 2. Make it the default
`Settings → Bluetooth & devices → Printers & scanners` → select the printer
→ **Set as default**. Turn OFF "Let Windows manage my default printer",
otherwise Windows will silently change it.

### 3. Set the paper size
In the driver's printing preferences, set the roll width (58 mm or 80 mm)
and set margins to 0. Match this to the tenant's
`receipt_paper_width_mm` setting in the manager dashboard.

### 4. Create the kiosk shortcut
Desktop → New → Shortcut:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --kiosk-printing --kiosk --app=https://<staff-pwa-url>/pos --user-data-dir=C:\nextgen-till-profile
```

* `--kiosk-printing` — suppresses the print dialog. **This is the flag that
  makes printing automatic.** Without it the cashier gets File → Print on
  every sale.
* `--kiosk` — full screen, no address bar.
* `--user-data-dir` — a dedicated profile, so the kiosk print setting and
  default printer cannot be disturbed by ordinary browsing.

### 5. Verify
Ring up a R0 test sale. The receipt should print with **no dialog at all**.

### Cash drawer on Path A
Check the printer driver's preferences for a "cash drawer" / "open drawer
before printing" option. Many Windows thermal drivers have one. **If it is
there, tick it and you do not need Path B at all.**

### Limits of Path A
- Prints only to the Windows *default* printer, so a separate kitchen
  printer is not possible.
- No drawer control unless the driver offers it.
- Output is driver-rendered, not byte-exact ESC/POS.

---

## Path B — local print bridge (only if Path A is not enough)

Install this when you need: a cash drawer the driver cannot open, a separate
kitchen printer, or byte-exact ESC/POS.

```
Browser POS ──HTTPS──> NextGen API        (fetches ESC/POS bytes)
     │
     └──HTTP, loopback only──> 127.0.0.1:9107 ──> thermal printer
```

### Install

1. Install Python 3.9+ on the till PC. The bridge uses **only the standard
   library** — nothing to `pip install`.
2. Copy `nextgen_print_bridge.py` to e.g. `C:\nextgen\`.
3. Generate a token:
   ```
   python -c "import secrets;print(secrets.token_urlsafe(32))"
   ```
4. First run writes a starter config and exits:
   ```
   python nextgen_print_bridge.py --config bridge-config.json
   ```
5. Edit `bridge-config.json` — paste the token, set the printer, and set
   `allowed_origins` to the exact staff PWA origin.
6. Run it. Add it to Startup so it survives a reboot.

### Config

```jsonc
{
  "token": "<the generated token>",
  "port": 9107,
  "allowed_origins": ["https://<staff-pwa-host>"],
  "printers": {
    "CUSTOMER_RECEIPT": {
      "transport": "windows",              // windows | tcp | file
      "windows_printer_name": "EPSON TM-T20III Receipt",
      "capabilities": {
        "supports_cut": true,
        "supports_drawer": true,
        "supports_qr": true,
        "codepage": "cp437"
      }
    },
    "KITCHEN": {
      "transport": "tcp",                  // LAN printer, raw port 9100
      "host": "192.168.1.50",
      "port": 9100
    }
  }
}
```

`transport` options:
- **windows** — writes RAW to a Windows print queue. Uses `pywin32` if
  present (true ESC/POS passthrough); otherwise falls back to copying to the
  printer share.
- **tcp** — raw port 9100, the standard for a LAN/Ethernet thermal printer.
- **file** — appends bytes to a file. Use for a USB device node, or to
  capture and inspect exactly what was sent while diagnosing.

### API

| Method | Path      | Auth   | Purpose |
|--------|-----------|--------|---------|
| GET    | `/health` | none   | Is the bridge up, and which profiles exist |
| POST   | `/print`  | bearer | `{profile, data_base64, copies}` |
| POST   | `/drawer` | bearer | `{profile}` — drawer kick only |

`/health` is deliberately unauthenticated and returns no secrets, so the PWA
can detect the bridge and decide whether to offer bridge printing.

### Getting the bytes

```
GET /v1/business/receipts/orders/{order_id}/escpos
      ?open_drawer=true
      &supports_cut=true&supports_drawer=true&supports_qr=false
      &document_type=CUSTOMER_RECEIPT
```

Capabilities are query parameters, not hardcoded per brand. A printer that
cannot cut, kick a drawer or render a QR simply does not receive those
bytes — instead of printing them as stray characters across the slip, which
is the usual failure when software assumes Epson and meets a clone.

### Security — read before deploying

- **Binds `127.0.0.1` only, never `0.0.0.0`.** This service prints and opens
  a cash drawer; it must not be reachable from the restaurant LAN or guest
  wifi. Verified with `netstat -ano | findstr 9107`.
- **Requires a per-device bearer token**, so another page in the same
  browser cannot trigger prints or pop the drawer.
- **CORS is an explicit allowlist.** An origin not in `allowed_origins` gets
  no CORS headers at all.
- **Answers Chrome's Private Network Access preflight**, which an HTTPS page
  needs before it may reach loopback. If prints fail only in Chrome with a
  CORS error in the console, this is the cause — confirm the PWA origin is
  in `allowed_origins` **exactly**, including scheme and any port.
- **Refuses to start with a token shorter than 16 characters.**
- Use a distinct token per till. Rotate by editing the config and
  restarting.

### If printing fails

The sale is already saved server-side before any printing is attempted, so a
printer failure never loses money. The bridge returns HTTP 502 with a
message and never retries by itself. Fix the printer, then use **Reprint**
on the order — which is audited and prints with a visible `REPRINT` banner.

---

## Choosing a path for Ciao Pizza

| Need | Path |
|---|---|
| Automatic customer receipt on the existing printer | **A** |
| Cash drawer, and the driver has a drawer setting | **A** |
| Cash drawer, and the driver does not | **B** |
| Separate kitchen printer | **B** |
| Byte-exact ESC/POS (logo, QR, precise cut) | **B** |

Path B is per-machine software to install, update and support. For a
single-till site that is a real ongoing cost, which is why Path A is worth
exhausting first.
