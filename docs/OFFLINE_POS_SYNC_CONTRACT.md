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
| `offline_review_if_too_old` | bool, default false | BUG-0208 (migration 0069). Opt in to the late-upload review queue: a capture refused **only** because it is older than the automatic age bound is stored for owner/manager review and answered `202 OFFLINE_CAPTURE_PENDING_REVIEW` instead of the 422. Only valid with `offline_captured_at` (422 otherwise). See section 10. |

`captured_by_user_id` and `offline_client_total_cents` without `offline_captured_at` are a 422 (pydantic).
Do not send a `business_id` or a till session id. The business comes from the JWT. The till session is chosen by the server.

Send the queued sale's original `items`, `payment_method` / `payments`, `discount_cents`, and
`cash_tendered_cents` as rung up. Pay-by-Link needs the network, so do not queue it.

## 2. What the server does

1. **Idempotent replay first.** If `(business, idempotency_key)` already exists, the original order is returned
   unchanged (HTTP 201 with the same body). Nothing is created, deducted or re-validated, so a retry after the
   72h window still succeeds. Concurrent identical requests are safe: exactly one order and one stock deduction.
   If the key is instead in the late-upload review queue (section 10), the answer is the review's: `202` while
   pending, the order once approved, `409 OFFLINE_CAPTURE_REVIEW_REJECTED` once rejected. Every offline upload of
   one key is serialised (a transaction-scoped advisory lock on business + key), so a key can never produce both
   an order and a review.
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
`late_sync` | `cashier_differs_from_syncer` | `post_event_grace_sync` | `late_capture_approved` |
`post_event_grace_upload` (omit for any flag), `include_acknowledged` (default false: orders an
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
| 403 | `BUSINESS_SUSPENDED` | the business is suspended at sync time and has no post-event sync grace (auth layer, section 10) |
| 403 | `EVENT_SYNC_ONLY` | event vendor inside its post-event sync grace: the request is not on the sync-only allowlist, or it is a `POST /pos/orders` without `offline_captured_at` (an online sale) |
| 202 | `OFFLINE_CAPTURE_PENDING_REVIEW` | (success, not an error) too old for automatic acceptance, opted in, queued for review; body carries `review_id`, `review_status`, `idempotency_key` |
| 409 | `OFFLINE_CAPTURE_REVIEW_REJECTED` | this key's late upload was rejected by a manager; final, do not retry |
| 409 | `OFFLINE_CAPTURE_REVIEW_DECIDED` | this key's review was decided but its order no longer exists; final |
| 409 | `OFFLINE_REVIEW_KEY_CONFLICT` | an online checkout reused a key that is queued for review |
| 422 | `INSUFFICIENT_CASH_TENDERED`, `PAYMENT_TOTAL_MISMATCH` | server total differs from what the cash/split payment covers |
| 404 / 422 | existing catalog errors | an item, option or variant was deleted or deactivated while offline |

Never mapped to a retry-forever loop: `OFFLINE_CAPTURE_*`, 404 and 403 will not succeed on retry.
Retry only on network errors and 5xx, with the same `idempotency_key`.
`OFFLINE_CAPTURE_TOO_OLD` is permanent: the sale needs a manager to enter it manually. A client that sends
`offline_review_if_too_old: true` gets `202 OFFLINE_CAPTURE_PENDING_REVIEW` instead and must treat the sale as
final on the device ("sent to manager for review"), never as an error and never as a retry.

## 7. Known limitations (owner decisions)

* **Suspended after the event (BUG-0208, resolved by section 10).** The hourly event reconcile job suspends an
  event-plan business once the event ends (`EVENT_SUBSCRIPTION_ENDED`). Before section 10 the auth layer then
  refused every request with `BUSINESS_SUSPENDED`, so a last-day sale still queued on a device could never
  upload. It now gets a sync-only grace (section 10); after the grace it is refused exactly as before.
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


## 10. Post-event sync grace and the late-upload review queue (BUG-0208, migration 0069)

Owner approval 2026-11-19. Code: `backend/app/events/trading.py` (`EVENT_SYNC_GRACE`,
`event_sync_grace_until`), `backend/app/events/sync_grace.py` (allowlist), `backend/app/core/rbac.py`
(`get_current_user`), `backend/app/services/auth_service.py` (sign-in), `backend/app/api/v1/routes_pos.py`
(review queue), `shared/models/offline_review.py`.

### 10.1 What did not change

* Automatic acceptance: EVENT plan 36h from capture, other plans 72h; an event capture day must be a paid TRADE
  day; 5 min future skew; every existing reason and flag. The age bound is **not** extended by the grace.
* Online trading after the vendor's paid days stays refused (the trading gate, and now also the allowlist).
* Clients that do not send `offline_review_if_too_old` get exactly the old behaviour (the old 422).
* Admin plane (`admin_api`) authentication is untouched.

### 10.2 The grace

`EVENT_SYNC_GRACE = 72h` (owner may adjust), measured from the end of the **anchor day**: the later of the
vendor's last paid TRADE day (latest billable day covered by a PAID subscription) and the event's last billable
day, end of that local date in the event's timezone. It is computed live from the database on every request (no
cached flag, no new column). A business is in grace only when ALL of these hold:

1. it is suspended (`is_active = false`) with **exactly** `suspended_reason = EVENT_SUBSCRIPTION_ENDED`;
2. it is on the EVENT plan;
3. at an event that is not CANCELLED, where its vendor row is not CANCELLED and has at least one paid TRADE day,
   `end(anchor) <= now < end(anchor) + 72h`.

Why that anchor (review L-3): the reconcile job suspends only when access becomes NONE, and for a vendor with any
paid day that happens after the event's **last** billable day (an unpaid day during the event is CONFIGURE, not a
suspension). Anchoring on the last paid day alone gave a vendor who paid only early days little or no grace. No
suspension timestamp is stored; the end of the event's last billable day is the earliest moment the automatic
suspension can happen (the hourly job may run up to an hour later).

An administrator's suspension (`EVENT_ADMIN_SUSPENDED`), a billing suspension, any other reason, a cancelled
vendor or event, and every non-event business never get grace. After the window: `BUSINESS_SUSPENDED` as before.

### 10.3 The sync-only allowlist (403 `EVENT_SYNC_ONLY` for everything else)

Matched on HTTP method + the FastAPI route template (never a client string):

| Method | Path (under `/v1`) | Why |
|---|---|---|
| GET | `/auth/me` | stay signed in (login, PIN login, staff directory, set-password, refresh and logout also work in grace; refresh/logout never checked suspension) |
| GET | `/business/event-access` | adds `sync_only: true` and `sync_only_until` (ISO UTC); `access_level` keeps its old value (`NONE`) |
| POST | `/business/pos/orders` | **only** when the JSON body has a non-null `offline_captured_at`; refused again in the route on the parsed body |
| GET | `/business/pos/offline-orders/flagged` | reconciliation status |
| POST | `/business/pos/offline-orders/{order_id}/acknowledge` | owner review marker (no money) |
| GET | `/business/pos/offline-reviews` | late-upload queue |
| POST | `/business/pos/offline-reviews/{review_id}/approve`, `/reject` | decide a late upload |
| GET | `/business/pos/till/current` | the session the offline sales belong to |
| POST | `/business/pos/till/{session_id}/close` | cash-up: records the counted cash and computes expected cash from sales already recorded; it creates no sale and moves no money. Opening a till is **not** allowed. |
| GET | `/business/orders`, `/business/orders/{order_id}` | read-only history |

**During grace every offline upload is held for review (review M-1, coordinator decision under the owner rule
"suspicious offline transactions require explicit review rather than silently adjusting financial records").** A
device clock can be backdated, so a capture that passes every hard check (timezone, not in the future, paid TRADE
capture day, active cashier, within the age bound or opted in to the queue when older) is NOT posted: it is stored
in the review queue (10.4) and answered `202 OFFLINE_CAPTURE_PENDING_REVIEW`, whether or not the client sent
`offline_review_if_too_old`. Nothing reaches orders, stock, payments, the till or loyalty until an owner/manager
approves it. The review carries `post_event_grace_upload` and `post_event_grace_sync` (never
`capture_day_unpaid_at_sync`), plus `late_sync` / `cashier_differs_from_syncer` when they apply; on approval the
order gets those plus `late_capture_approved`. A too-old capture in grace without the opt-in flag is still the old
422. Note for clients released before this field: a 202 has no order id, so they must not treat it as an order
(the current staff PWA shows "Sent to manager for review").

### 10.4 Late-upload review queue

When `offline_review_if_too_old: true` and the capture is refused **only** because of the age bound, the server
first re-runs every other check (timezone, not in the future, an event capture day must be a paid TRADE day, the
cashier is an active user of this business). Any of those failing is the usual hard refusal and nothing is
queued. Otherwise it stores the validated request body in `offline_capture_reviews` (tenant-scoped, unique on
`(business_id, idempotency_key)`) and answers:

```
202 {"code": "OFFLINE_CAPTURE_PENDING_REVIEW", "review_id": "...", "review_status": "PENDING",
     "idempotency_key": "...", "message": "..."}
```

A pending review posts **nothing** to orders, stock, payments or the till. Restaurants (non-event plans) get the
same queue for >72h captures when their client opts in: approval re-validates everything except the age bound,
so the only effect is that a manager, not a timer, decides an old sale.

Endpoints (owner or manager; business from the JWT; POS entitlement required):

* `GET /v1/business/pos/offline-reviews?status=PENDING|APPROVED|REJECTED|ALL&limit=1..200` (default PENDING, 50)
  returns `{items: [...], pending_count}`. Each item: `id, status, idempotency_key, captured_at, received_at,
  captured_by_user_id/_name, synced_by_user_id/_name, upload_review_reasons, payment_method, client_total_cents,
  discount_cents, lines [{menu_item_id, variant_id, quantity, name}], decided_by_user_id/_name, decided_at,
  decision_note, order_id`. `pending_count` is the business's total pending, whatever the filter.
* `POST /v1/business/pos/offline-reviews/{id}/approve` body `{"note": "..."}` (1-1000 chars, required) returns
  `{review, order}`. The order is created through the **same** code path as a normal offline sync, re-validating
  everything except the age bound: event capture day still a paid TRADE day, server price, variants and stock as
  normal, till by capture instant (else `offline_unattributed`). It carries the review's idempotency key and the
  reasons judged at upload plus `late_capture_approved`, so it is in the flagged list and the organiser's
  flagged count. H-1: the discount limit uses the **original uploader's** role (its upload-time role, lowered to
  its current role; an uploader no longer active counts as STAFF, review L-1) and the cashier's; the approver
  never raises it, and `discount_approved_by_user_id` stays the uploader. The cashier is the one validated at
  upload (`captured_by_user_id`, same business and active then); it must still belong to the business but may
  have been deactivated since, in which case its authority is STAFF (review L-2). A failed re-validation leaves
  the review PENDING. A stored payload that no longer validates is `422 OFFLINE_REVIEW_PAYLOAD_INVALID`.
* Price on approval: exactly like a normal offline sync, the order is priced at the **current** menu price when
  it is recorded (approval time), never the device's. The device total (`offline_client_total_cents`) is kept and
  any difference is recorded as `offline_price_variance_cents` with the sale flagged, so the manager sees it.
* An ONLINE checkout whose `idempotency_key` is queued for review is refused with
  `409 OFFLINE_REVIEW_KEY_CONFLICT` (same key lock as offline uploads), so a review is never stranded.
* The organiser sees `pending_offline_reviews` per vendor on `GET /admin/events/{id}/vendors` (read-only), so
  reviews left pending after the grace are visible.
* The admin generic suspend (`POST /admin/businesses/{id}/suspend`) refuses the reserved reasons
  `EVENT_SUBSCRIPTION_ENDED` and `EVENT_ADMIN_SUSPENDED` (422 `SUSPENSION_REASON_RESERVED`).
* `POST /v1/business/pos/offline-reviews/{id}/reject` body `{"note": "..."}` records the decision; no financial
  effect.
* Both lock the review row: two concurrent approvals make exactly one order; approving an approved review (or
  rejecting a rejected one) returns it unchanged; approve-after-reject or reject-after-approve is
  `409 OFFLINE_REVIEW_ALREADY_DECIDED`. Another business's review id is `404 OFFLINE_REVIEW_NOT_FOUND`.
* Audit events: `order.pos.offline_review_queued`, `order.pos.offline_review_approved`,
  `order.pos.offline_review_rejected`.

Not bounded: a restaurant can queue a capture of any age (the manager decides); an event vendor's queue is bounded
in practice by its paid TRADE days. If that proves noisy, add a hard maximum age for queueing.
