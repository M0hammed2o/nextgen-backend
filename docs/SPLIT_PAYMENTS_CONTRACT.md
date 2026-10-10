# Split Payments + WhatsApp Pay-by-Link — Locked Contract

Branch: `feat/pos-split-payments` (backend/staff/manager). Migration: `0055_pos_payment_allocations`.
Status: **DESIGN LOCKED, implementation in progress.** This file is the single source of truth every
agent/session working on this feature must follow — do not invent a divergent shape.

## Verified provider support matrix (audited, not assumed)

| Provider | Webhook correlation field | Webhook carries amount+currency? | POS Pay-by-Link safe? |
|---|---|---|---|
| YOCO | `metadata.orderId` (free text, set by us) | Yes (`inner.amount`/`inner.currency`) | **YES** |
| PAYFAST | `m_payment_id` (free text, set by us) | Yes (`amount_gross` parsed to cents; ZAR hardcoded, correct) | **YES** |
| IKHOKA | `externalTransactionID` (free text, set by us) | **No — KNOWN GAP, pre-existing**, `amount_cents=None` always | **NO** |
| STITCH | `externalReference` (free text, set by us) | **No — KNOWN GAP, pre-existing**, `amount_cents=None` always | **NO** |
| DIRECT_EFT | n/a (no webhook) | n/a | NO (manual) |
| MOCK | `payload.order_id` | being extended to carry amount/currency for tests | YES (test-only) |

All 4 real providers' `verify_payment()` is an unimplemented stub (`return False`) — there is no live-query
fallback for IKHOKA/STITCH's missing amount. Without a verified amount, a webhook cannot safely confirm a POS
Pay-by-Link intent (the whole point is exact-amount verification), so IKHOKA/STITCH are correctly excluded,
not "not yet gotten to." If a business's configured provider is IKHOKA/STITCH/DIRECT_EFT, Pay by Link is
unavailable in the POS (precise disabled state), while remaining usable for the existing WhatsApp-order flow
this change does not touch.

## Webhook correlation mechanism (reuses 100% of existing signature verification)

A POS intent's `create_payment_link` call sets the provider's free-text order-reference field to
`f"pos_intent:{intent.id}"` instead of `str(order.id)`. `routes_payments.py`'s existing handlers (unchanged
signature verification) check this prefix on the parsed `order_id` result and branch to a new
`_mark_intent_paid()` (mirrors `_mark_order_paid()`) instead of looking up an `Order`. No order exists yet for
a POS intent, so this prefix is the only safe way to keep using the SAME webhook endpoints or this would be
"a second unrelated payment stack."

## Schema (migration 0055, one migration — nothing has shipped yet)

### `pos_payment_allocations` — FINAL, CONFIRMED, IMMUTABLE money (never updated after insert)
Every POS order created after this migration gets >=1 row here, including an ordinary single-method sale —
one code path for till cash-up/analytics/receipts, not two.

```
id, business_id FK CASCADE, order_id FK CASCADE, till_session_id FK SET NULL (nullable)
payment_method  CASH | CARD | OTHER | PAY_BY_LINK
amount_cents    > 0, portion of order total this allocation settles
cash_tendered_cents, change_due_cents   CASH only
payment_reference   CARD/OTHER/PAY_BY_LINK (provider's own txn ref for the latter)
provider           PAY_BY_LINK only — YOCO | PAYFAST
payment_link_intent_id  FK pos_payment_link_intents SET NULL, PAY_BY_LINK only
paid_at             PAY_BY_LINK only — when the underlying intent was confirmed paid
created_by_user_id, created_at
```

### `pos_payment_link_intents` — STATEFUL, async (PENDING -> PAID -> CONSUMED | FAILED | EXPIRED | CANCELLED)
Deliberately a SEPARATE table from allocations — an intent is mutable while pending, an allocation never is.

```
id, business_id FK CASCADE, till_session_id FK SET NULL (nullable), created_by_user_id
amount_cents > 0, currency (default ZAR)
provider   YOCO | PAYFAST only (CHECK)
status     PENDING | PAID | FAILED | EXPIRED | CANCELLED | CONSUMED  (CHECK)
provider_reference, payment_link_url (url NEVER returned to the Staff client)
customer_phone (E.164), whatsapp_status (NOT_SENT|SENT|FAILED|TEMPLATE_REQUIRED),
whatsapp_message_id, whatsapp_failure_reason
order_id FK orders SET NULL, UNIQUE where not null — a consumed intent points to exactly one order
created_at, expires_at (created_at + 15 min), paid_at, consumed_at, cancelled_at
CHECK: status != 'CONSUMED' OR order_id IS NOT NULL
```

Consumption is concurrency-safe via `SELECT ... FOR UPDATE` on the intent row inside the checkout transaction
(same pattern as `_get_open_till`/`close_till`'s existing row locks), transitioning PAID -> CONSUMED exactly
once; a second concurrent attempt sees the already-CONSUMED status and is rejected.

## Checkout API (`POST /v1/business/pos/orders`)

Legacy single-payment payload (top-level `payment_method`/`cash_tendered_cents`/`payment_reference`) is
**100% unchanged** and still the only thing the current Staff build sends.

New alternate shape — `payments: list[...]`, >= 2 entries, mutually exclusive with the legacy top-level fields:

```json
{
  "payments": [
    {"payment_method": "CASH", "amount_cents": 15000, "cash_tendered_cents": 20000},
    {"payment_method": "PAY_BY_LINK", "amount_cents": 30000, "payment_intent_id": "..."},
    {"payment_method": "CARD", "amount_cents": 15000, "payment_reference": "terminal-ref"}
  ]
}
```

Server-side, after the existing discount/loyalty/VAT pipeline computes the real `total_cents` (unchanged):
`SUM(payments[].amount_cents)` must equal `total_cents` exactly; every `PAY_BY_LINK` entry's intent must be
this business's own, `status == PAID`, not already `CONSUMED`, and `intent.amount_cents == allocation.amount_cents`
exactly — then intents are locked+flipped to `CONSUMED` and allocation rows created, all inside the SAME
all-or-nothing transaction as the order/items/inventory/loyalty, exactly like today's single-payment path.
Idempotent retry (existing `idempotency_key` mechanism, unchanged) returns the original order before any of
this runs again.

## Payment-intent API (new)

```
POST   /v1/business/pos/payment-intents      {amount_cents, customer_phone} -> safe state, never the raw URL
GET    /v1/business/pos/payment-intents/{id} -> safe state (for polling, 2-3s interval while dialog open)
POST   /v1/business/pos/payment-intents/{id}/resend  -> re-send WhatsApp for the SAME link, rate-limited
POST   /v1/business/pos/payment-intents/{id}/cancel  -> PENDING only
```

## WhatsApp delivery

Reuses `backend/app/bot/whatsapp_sender.py::send_template_message` (the exact mechanism
`receipts/whatsapp_receipt.py::send_digital_receipt` already uses for the identical 24-hour-window problem) —
a platform-default template name/language (`settings.POS_PAYMENT_LINK_TEMPLATE_NAME`, new) with a per-business
override (`Business.whatsapp_payment_link_template_name`, new), same pattern as the receipt template fields.

**Honest disclosure, not assumed working**: whether any such template is actually Meta-approved for a given
WABA is unverified in this session — same as the pre-existing receipt template's own unverified status. The
send is attempted for real (never faked); a template-not-found/not-approved error from Meta is classified via
the existing `meta_errors` machinery and surfaced as `whatsapp_status=TEMPLATE_REQUIRED` with a precise Staff
message, never a false "Sent."

## Till cash-up

`close_till` expected-cash only ever includes CASH allocations — PAY_BY_LINK (and CARD/OTHER) never touch the
drawer, exactly like CARD today.

## Analytics

Payment-mix groups by `pos_payment_allocations.payment_method` (gross, always exact) — never "SPLIT" as a
category. PAY_BY_LINK can optionally break down by `provider` in a deeper view.

## No real external side effects, ever, in this work

`MockPaymentProvider` (already exists, already the established test-only pattern) is used for every
local/automated test. No real Yoco/PayFast link is created, no real WhatsApp template send, no real webhook
against a live provider, anywhere in this branch's history.
