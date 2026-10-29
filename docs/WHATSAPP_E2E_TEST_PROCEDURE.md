# Controlled real-WhatsApp end-to-end test procedure

**Status: BLOCKED in this environment, not executed, not claimed as passed.**
This session has no Meta/WhatsApp Business API sandbox credentials, no
verified webhook endpoint reachable from Meta, and no live `WHATSAPP_DEFAULT_ACCESS_TOKEN`
(see `.env.backend.example` — `WHATSAPP_DEFAULT_ACCESS_TOKEN`, `META_VERIFY_TOKEN`,
`META_APP_SECRET`). Per the WhatsApp ordering reliability directive's own
instruction ("Do not claim that production WhatsApp has been verified"),
this document is the **procedure to run**, not a report of a run that
happened. Nothing in this file should be read as evidence that real
WhatsApp delivery was exercised.

This procedure is written so a session (or person) that DOES have sandbox
access can execute it directly, and so the exact gap is visible to anyone
reviewing this branch before a release decision.

## 1. Prerequisites (all currently missing in this environment)

1. A Meta Business/App with WhatsApp enabled, in sandbox or test mode.
2. A test WhatsApp Business phone number registered to that app.
3. `WHATSAPP_DEFAULT_ACCESS_TOKEN` — a valid system-user access token with
   `whatsapp_business_messaging` + `whatsapp_business_management` scopes.
4. `META_APP_SECRET` and `META_VERIFY_TOKEN` configured and matching the
   webhook subscription set up in the Meta App dashboard.
5. A publicly reachable HTTPS endpoint for `WHATSAPP_WEBHOOK_PATH`
   (`/v1/webhook/meta`) — e.g. a staging deployment, or a tunnel (ngrok/
   Cloudflare Tunnel) pointed at a local run of this branch for a
   throwaway test business.
6. A real test business row in the target database, with
   `whatsapp_phone_number_id` set to the sandbox number's phone_number_id,
   `is_active=True`, and a small real menu (reuse the Mochachos-style
   catalog already in this branch's fixtures, or a dedicated disposable
   test catalog — never a live production business).
7. A real phone capable of sending/receiving WhatsApp messages to that
   sandbox number (the tester's own phone, or a second test number).

## 2. Safety constraints (must hold for the entire procedure)

- The target business MUST be a disposable test business, never a live
  production tenant. Confirm this positively before sending a single
  message (`SELECT is_active, name FROM businesses WHERE id = ...` and
  visually confirm it is the throwaway test row, not a real customer).
- No real payment provider may be connected to the test business — if
  `online_payment_required` is exercised, use a sandbox/test payment
  provider key only.
- Do not use a WhatsApp number that is also used for any real production
  Mochachos (or other live tenant) traffic.
- After the run, the test business's orders/sessions/messages must be
  deleted (or the whole test business dropped) — this procedure creates
  real rows in whatever database it targets, so never point it at the
  production database.

## 3. Procedure

Run each scenario below as a REAL WhatsApp conversation (typing on a real
phone, waiting for real delivery), not a simulated/mocked call. For each
scenario, capture: the exact message text sent, the exact reply received
(screenshot or copy-pasted text with timestamp), and the persisted DB
state immediately after (`cart`, and after checkout, the `orders` /
`order_items` rows) queried directly against the target database.

1. **Golden path**: "Hi" → order a known single item with no modifiers →
   "Yes" → choose pickup → provide any required details → confirm checkout.
   Verify: exactly one `Order` row, correct `subtotal_cents`/`total_cents`,
   correct `OrderItem` snapshot.
2. **Required-modifier item**: order an item with a required option group
   (e.g. spice level) → verify the bot asks for it exactly once → answer →
   verify the resulting cart line carries the right `special_instructions`.
3. **Mid-order correction**: start an order, then send a natural-language
   quantity correction ("actually make that 3, not 1") → verify the real
   cart quantity updates and the bot's reply matches the real total, not a
   hallucinated one.
4. **Unavailable item**: ask for an item not on the test menu → verify the
   bot does NOT silently add anything, and the real cart stays unchanged.
5. **Webhook redelivery**: this step can't be forced from the phone side
   (Meta's actual retry behaviour is opaque), but if a reply is visibly
   delayed and the phone's WhatsApp client silently resends, confirm
   afterward that only one `Message`/cart mutation was persisted for that
   turn (same invariant proven synthetically in
   `tests/integration/test_whatsapp_pipeline_concurrency.py`).
6. **Full checkout with real details**: complete an entire pickup order
   through to `ORDER_PLACED`, providing a real name/phone number, and
   verify the final `Order` row's `customer_name`/`phone_number` match
   exactly what was typed on the phone.

## 4. Pass/fail criteria

A scenario PASSES only if the real, queried database state matches what
the bot's reply claimed — not merely that a reply arrived. Any mismatch
between the bot's WhatsApp reply and the actual persisted cart/order is an
automatic FAIL for that scenario and must be filed as a new entry in
`KNOWN_BUGS.md` with the exact message text and timestamp, the same way
the original Mochachos transcript bugs (BUG-0157/0158/0159) were found and
fixed this session.

## 5. What this replaces vs. what it doesn't

The synthetic integration suite (`tests/integration/`, including
`test_whatsapp_pipeline_concurrency.py` and
`test_acceptance_scenarios.py`) already proves the pipeline's logic against
a real Postgres database with a simulated inbound webhook call — it does
NOT prove: real Meta webhook signature verification end-to-end, real Meta
API rate limiting/retry behaviour, real message delivery latency, or that
the currently-deployed production WhatsApp number's actual Meta
configuration (app review status, message templates, business
verification) is healthy. Only this procedure, run against real sandbox
access, proves those things. Until it is run, that risk remains
**open and explicitly disclosed**, not silently assumed to be fine.
