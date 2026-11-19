# Offline POS sync contract (Phase 3b)

For the staff PWA. It queues POS sales in IndexedDB while offline and replays each one to
`POST /v1/business/pos/orders` on reconnect. The backend records the sale as it happened. It never lets
the client decide a price.

Migration `0067_offline_pos_capture`. Code: `backend/app/api/v1/routes_pos.py` (`create_pos_order`),
`backend/app/pos/offline.py`, `backend/app/events/trading.py`.

## 1. Request: new optional fields on `POST /v1/business/pos/orders`

Everything else on the body is unchanged (items, payment_method / payments, discount, loyalty, ...).

| Field | Type | Meaning |
|---|---|---|
| `idempotency_key` | string, max 128 | **Required** when `offline_captured_at` is sent. Generate once per queued sale, reuse on every retry. |
| `offline_captured_at` | ISO-8601 datetime **with timezone offset** | When the sale was rung up on the device. Presence makes this an offline capture. |
| `is_offline_capture` | bool, optional | Implied `true` by `offline_captured_at`. `true` without it, or `false` with it, is a 422. |
| `captured_by_user_id` | uuid, optional | The cashier who made the sale. Must be an active user of the caller's business. Default: the syncing caller. |
| `offline_client_total_cents` | int >= 0, optional | The total the device displayed. Informational only (see section 4). |

`captured_by_user_id` and `offline_client_total_cents` without `offline_captured_at` are a 422 (pydantic).
Do not send a `business_id` or a till session id. The business comes from the JWT. The till session is chosen by the server.

Send the queued sale's original `items`, `payment_method` / `payments`, `discount_cents`, and
`cash_tendered_cents` as rung up. Pay-by-Link needs the network, so do not queue it.

## 2. What the server does

1. **Idempotent replay first.** If `(business, idempotency_key)` already exists, the original order is returned
   unchanged (HTTP 201 with the same body). Nothing is created, deducted or re-validated, so a retry after the
   72h window still succeeds. Concurrent identical requests are safe: exactly one order and one stock deduction.
2. **Capture window.** `offline_captured_at` must be within `[now - 72h, now + 5min]`. For a business on the
   EVENT plan the lower bound is **36h** (`EVENT_OFFLINE_CAPTURE_MAX_AGE`), not 72h (`details.max_age_hours`
   reports the bound that applied). **Owner may adjust** this and the two thresholds in section 8.
3. **Trading gate (event vendors only).** Judged on the capture's local date in the event's own timezone, not
   on the sync date. A sale made on a paid day and synced the next morning (or after the event ended) passes at
   this gate. A sale made on an unpaid or non-event day is refused even if synced on a paid day. Non-event
   businesses are unaffected. See the limitation in section 7.
4. **Cashier.** `captured_by_user_id` is checked against the caller's own business. The order's
   `created_by_user_id` is the cashier. The syncing user is `offline_synced_by_user_id`. A staff discount
   limit and every other role check use the **lower** of the syncer's role and the cashier's role (a staff
   syncer naming the owner gets staff limits; an owner syncing a staff member's sale gets staff limits).
   `discount_approved_by_user_id` is always the syncing caller. When the cashier is not the syncer the
   sale gets review reason `cashier_differs_from_syncer`. A shared till device syncing a colleague's queued
   sales stays supported.
5. **Till session.** The sale is attached to the till session of this business that was open at
   `offline_captured_at` (`opened_at <= captured <= closed_at`, or still open), not to whatever is open at
   sync time. Payment allocations carry the same session, so cash-up totals follow it. The till does not
   have to be open at sync time. If no session covers the instant, `till_session_id` is `null` and
   `offline_unattributed = true`.
6. **Stock.** The sale is never refused for stock. The ledger movements are posted as they would be, dated at
   `offline_captured_at`. If any ingredient/variant stock ends below zero, `offline_stock_shortfall = true`.
   (Online sales behave as before.)
7. **Status.** An offline sale is created `COMPLETED` / `PAID`, never `ACCEPTED`, because it has already
   happened and a stale kitchen ticket would only clutter the kitchen screen. `paid_at`, `confirmed_at`,
   `completed_at` and payment allocation times are the capture time. `created_at` is the sync time.
   Analytics bucket the sale at `offline_captured_at`.

## 3. Response

The usual `OrderResponse`, plus:

| Field | Meaning |
|---|---|
| `is_offline_capture` | true for an offline capture |
| `offline_captured_at` / `offline_synced_at` | capture time (UTC) / server receive time |
| `created_by_user_id` | the cashier (`captured_by_user_id`, or the caller by default) |
| `offline_synced_by_user_id` | the user whose session synced it |
| `till_session_id` | session that covered the capture instant, or `null` |
| `offline_unattributed` | no till session covered the capture instant |
| `offline_late_to_closed_till` | attributed to a session that had already closed when the sale synced (its stored cash-up predates this sale) |
| `offline_stock_shortfall` | stock went negative |
| `offline_client_total_cents` | the client-reported total, as sent |
| `offline_price_variance_cents` | client minus server; present only when they differ |
| `offline_review_required` | any of the four flags above is set |

`total_cents` is always the server's figure. If `offline_price_variance_cents` is set, the receipt the
device printed offline differs from the server's price. The device should show the server total after sync.

## 4. Price is server-authoritative

The server prices from the live catalog (variant price overrides included). The client never sets a price.
If `offline_client_total_cents` differs from the server total, the order is kept at the server total,
`offline_price_variance_cents = client - server`, and `offline_review_required = true`. The client figure is
never used for payment validation or revenue.

Because the payment is validated against the server total, an offline cash sale whose `cash_tendered_cents`
is less than the server total is refused with `INSUFFICIENT_CASH_TENDERED`. If a price rose while the device
was offline, the PWA must surface the failure to the user and not drop the queued sale silently.

## 5. Manager review list

`GET /v1/business/pos/offline-orders/flagged` (owner or manager only; the business comes from the JWT).

Query: `reason` = `shortfall` | `variance` | `unattributed` | `late_till` | `capture_day_unpaid_at_sync` |
`late_sync` | `cashier_differs_from_syncer` (omit for any flag), `include_acknowledged` (default false: orders an
owner has acknowledged are hidden), `since` (ISO datetime, filters on `offline_captured_at`), `limit` (1-200,
default 50).
Returns, newest capture first, a list of: `id, order_number, total_cents, payment_method,
offline_captured_at, offline_synced_at, created_by_user_id, offline_synced_by_user_id, till_session_id,
offline_unattributed, offline_late_to_closed_till, offline_stock_shortfall, offline_client_total_cents,
offline_price_variance_cents, review_reasons, offline_acknowledged_at, offline_acknowledged_by_user_id`.
`review_reasons` lists every reason (boolean flags plus the migration 0068 reasons).

`POST /v1/business/pos/offline-orders/{order_id}/acknowledge` (**owner only**, business from the JWT; another
business's order, or one that is not a flagged offline order, is a 404) records the reviewer and time on the
order, writes an `order.pos.offline_acknowledged` audit event and hides the order from the default list and
from the till-close unattributed totals. It changes no money and no stock; a second call keeps the first
reviewer and time.

Cash-up: `POST /v1/business/pos/till/{id}/close` now also returns `offline_unattributed_count` and
`offline_unattributed_cash_cents`: **every** not-yet-acknowledged offline sale of this business that no session
covered (not only those since the previous close). Their cash is not part of `expected_cash_cents`, so the
manager must reconcile it, and an owner's acknowledgement is what removes a sale from the count.

Refunds: a cash refund or void of an offline sale that is `offline_unattributed` or
`offline_late_to_closed_till` is refused unless the caller is an OWNER (403 `OFFLINE_REFUND_REQUIRES_OWNER`):
that cash is in no cash-up, so handing it back inside today's session would show zero variance.

## 6. Error codes

| HTTP | code | When |
|---|---|---|
| 422 | `OFFLINE_CAPTURE_TOO_OLD` | `offline_captured_at` is more than 72h before the server clock (`details.max_age_hours`) |
| 422 | `OFFLINE_CAPTURE_IN_FUTURE` | more than 5 min after the server clock (device clock is wrong) |
| 422 | `OFFLINE_CAPTURE_NO_TIMEZONE` | `offline_captured_at` has no UTC offset |
| 422 | validation error | offline-only field without `offline_captured_at`; `is_offline_capture` contradicting it; missing `idempotency_key` |
| 404 | `USER_NOT_FOUND` | `captured_by_user_id` is not an active user of this business (same answer for another business's user) |
| 422 | `OFFLINE_CAPTURE_TOO_OLD` (event plan) | more than 36h before the server clock |
| 403 | `OFFLINE_REFUND_REQUIRES_OWNER` | cash refund/void of an unattributed or late-to-closed-till offline sale by a non-owner |
| 422 | `ORDER_TOTAL_TOO_LARGE` | priced lines sum past the INTEGER column (all amount fields are bounded at 2147483647, a 422 not a 500) |
| 403 | `EVENT_TRADING_NOT_ALLOWED` | event vendor: the capture date was not a paid trading day (`details.reason`) |
| 403 | `BUSINESS_SUSPENDED` | the business is suspended at sync time (auth layer, see section 7) |
| 422 | `INSUFFICIENT_CASH_TENDERED`, `PAYMENT_TOTAL_MISMATCH` | server total differs from what the cash/split payment covers |
| 404 / 422 | existing catalog errors | an item, option or variant was deleted or deactivated while offline |

Never mapped to a retry-forever loop: `OFFLINE_CAPTURE_*`, 404 and 403 will not succeed on retry.
Retry only on network errors and 5xx, with the same `idempotency_key`.
`OFFLINE_CAPTURE_TOO_OLD` is permanent: the sale needs a manager to enter it manually.

## 7. Known limitations (owner decisions)

* **Suspended after the event.** The hourly event reconcile job suspends an event-plan business once the
  event ends (`EVENT_SUBSCRIPTION_ENDED`). The auth layer then refuses every request with
  `BUSINESS_SUSPENDED` before the trading gate runs. A sale made on the last paid day and synced after the
  suspension is therefore refused, even though the gate itself would accept it. Fixing it needs a grace window
  in `get_current_user` (a security-relevant change), which this phase deliberately did not make.
* **Device clock skew.** The capture instant is the device's clock. If the till session was opened a few
  seconds "after" the device clock says the sale was made, the sale is `offline_unattributed` and shown to
  the manager rather than put in the wrong session.
* **Late to a closed session.** A sale attributed to an already-closed session does not change that session's
  stored `expected_cash_cents` / `variance_cents` (a closed cash-up is never rewritten). It is flagged
  `offline_late_to_closed_till` so the manager can see why that session's counted cash exceeded its expected cash.
* **Acknowledge is a review marker only.** It does not clear the flag columns (`offline_review_required`
  stays true so the history remains); it only hides the order from the default list and the totals.

## 8. Event-vendor backdating (souk review H-2) and its defaults

A vendor on a CONFIGURE-only (unpaid) day can log in and sync, and a device timestamp cannot be proven. The
layered defaults below limit and expose the abuse; they are **defaults the owner may adjust**
(`backend/app/events/trading.py`):

1. EVENT-plan businesses: max capture age `EVENT_OFFLINE_CAPTURE_MAX_AGE = 36h` (other plans stay 72h).
2. EVENT-plan businesses: if the access decision **at sync time** is not TRADE, a capture that passes the gate
   is still accepted but flagged `capture_day_unpaid_at_sync`.
3. Any plan: a capture more than `OFFLINE_LATE_SYNC_THRESHOLD = 12h` older than the sync is flagged `late_sync`.
4. The organiser sees the count of unacknowledged flagged offline orders per vendor:
   `GET /admin/events/{id}/vendors` items carry `flagged_offline_orders` (admin plane, platform-wide).

Reasons not already a boolean are stored in `orders.offline_review_reasons text[]` (migration 0068).

## 9. Related souk-review items (same migration/PR)

* **Public registration (M-2).** `POST /v1/public/events/{slug}/register` now decides access for the newly
  created business only (`reconcile_single_business_access`), never the global reconcile, so a public request
  writes no other tenant's rows. A per-event ceiling (`EVENT_REGISTER_RATE_LIMIT_PER_EVENT_PER_HOUR`, default
  30/hour, 429 `EVENT_REGISTRATION_RATE_LIMITED`) is spent only by a request that already holds the valid
  registration code. **Launch item, not done: email verification of the owner address at registration.**
* **Payment claims (L-2).** The vendor's claimed reference is recorded in the `event.subscription_payment_claimed`
  audit diff, and the admin vendor list marks `payment_claim_shared: true` on a subscription whose normalised
  reference is also claimed by another live subscription at the same event.
* **Event plan and WhatsApp (L-4).** Admin create/patch refuse `is_whatsapp_enabled=true` and any
  `whatsapp_phone_number_id` for an EVENT-plan business (422 `EVENT_PLAN_WHATSAPP_NOT_ALLOWED`); moving a
  business to EVENT forces `is_whatsapp_enabled=false`, and requires the number to be cleared in the same request.

