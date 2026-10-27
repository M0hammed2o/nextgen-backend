# Marketplace integrations — Uber Eats and Mr D

Operations reference for the delivery-marketplace module (`backend/app/delivery/`,
`backend/app/external_orders/`). It covers what works today, how to configure
and onboard a restaurant, how to verify a pilot, and what it costs.

Written 2026-10-26, branch `feat/marketplace-integrations`. API facts below come
from Uber's developer docs (developer.uber.com/docs/eats) as read on 2026-10-26.

## 1. Status at a glance

| Channel | What works | Live connectivity |
|---|---|---|
| **Uber Eats, live API** | Built and covered by automated tests: webhook intake, order fetch, menu mapping, accept/deny, kitchen hand-off, ready-for-pickup, restaurant cancel, store pause/resume, menu import for mapping | **Sandbox authentication verified 2026-10-27.** The sandbox app's `client_credentials` token was minted from `https://sandbox-login.uber.com/oauth/v2/token` through the adapter's own code: HTTP 200, Bearer, `expires_in` 2,592,000 s, all four requested scopes granted (`eats.order`, `eats.store`, `eats.store.orders.read`, `eats.store.status.write`). **Everything past authentication is still unverified**, because the test store is pending (Uber support ticket #69BF76); those calls have only been tested against Uber's documented payloads with the network mocked. |
| **Uber Eats, CSV report** | Working (`/delivery-orders` import). A report order already booked through the live API is skipped as a duplicate. | n/a |
| **Mr D, live API** | Disabled adapter only. Mr D publishes no partner API, and NextGen has no credentials. | **Pending Partner Approval** |
| **Mr D, CSV report** | Working (same importer) | n/a |

"Connected" may only appear in any UI when `delivery_connections.status = CONNECTED`. A SUPER_ADMIN sets that, and only with Uber's written approval reference recorded (see §4).

## 2. How an Uber Eats order flows

1. Uber sends a webhook to `POST /v1/webhook/delivery/UBER_EATS`. The signature (`X-Uber-Signature`, a lowercase hex HMAC-SHA256 of the raw body) is checked against the production secret, then the sandbox secret. Whichever matches **proves the environment**; the `X-Environment` header is only logged.
2. The tenant is resolved only from `(provider, store id) → delivery_connections`. Nothing else in the webhook can choose a business.
3. The worker fetches the order and checks four gates in turn: environment matches, tenant active, entitlement on, connection may take orders. A known store that fails any gate gets an **active deny**, never silence.
4. Items and modifiers are resolved against `external_product_mappings`, keyed by Uber's entity id. That includes add-ons, sizes/options and removed ingredients. Exact-name matches are automatic; nothing fuzzier is.
   - Unmapped → `NEEDS_MAPPING`: no sale, shown to staff and managers, auto-denied at the deadline.
   - An 86'd item (`is_active=false`) → denied with `ITEM_AVAILABILITY`.
5. Fully mapped → `READY_TO_ACCEPT` (the default), or accepted automatically if the owner turned auto-accept on.
6. Staff accept from the Staff PWA's Live Orders. One transaction then creates the real `Order`, deducts stock once and calls Uber accept. The kitchen sees the order only after that commit.
   - Uber says no (4xx) → everything rolls back; no sale, no stock.
   - No answer from Uber after retries → `ACCEPT_UNCONFIRMED`. The kitchen keeps the order, the watchdog never denies it, and staff can "Retry with Uber Eats" or a manager can confirm it from the Uber tablet.
7. The kitchen's own status changes are pushed back to Uber in the background:
   - READY → Uber "ready for pickup"
   - CANCELLED → restaurant cancel
   - COLLECTED/DELIVERED → closes the delivery row
   A failed push is shown on the order and never undoes the kitchen's action.
8. When Uber cancels, the order is cancelled with the platform-wide stock rule (`inventory/cancellation.py`):
   - kitchen not started → stock restored, exactly once
   - kitchen started → deduction kept, with an audit entry
   - already handed over → the sale stands as recorded

Every `delivery_orders.status` change writes an `AuditEvent` (`delivery.order.status_changed`, or the staff action's own name).

### What a marketplace order books

| Order field | Source (Uber's own definition) |
|---|---|
| `subtotal_cents` | `sub_total`: "the total cost of each line item" |
| `delivery_fee_cents` | `total_fee`: fees "paid out to the Merchant" |
| `total_cents` | `total`: "the entire order … paid out to the Merchant excluding Marketplace Fees" |
| `tax_cents` | VAT derived from inside the total, as for every other channel. Uber's own `tax` figure is kept in the order snapshot for reconciliation. |
| `payment_method` / till | `EXTERNAL_PLATFORM`, no till session. It can never reach cash-up, and payment or refund edits are refused (`EXTERNAL_ORDER_PAYMENT_LOCKED`, BUG-0156). |
| lines | Uber's per-unit and line prices. These are the channel's own prices and can differ from the in-store menu. |

How Uber reports tax and merchant-funded promotions for a **South African** store is not shown in Uber's (US) examples. Check the first real sandbox order against the restaurant's Uber payout statement.

## 3. Configuration (backend env vars)

| Variable | Purpose |
|---|---|
| `UBER_EATS_WEBHOOK_ENABLED` | Platform kill switch. Default `false`. |
| `UBER_EATS_CLIENT_ID` / `UBER_EATS_CLIENT_SECRET` | **Production** app. |
| `UBER_EATS_API_BASE_URL` / `UBER_EATS_AUTH_BASE_URL` | Production hosts. Defaults: `https://api.uber.com`, `https://auth.uber.com`. |
| `UBER_EATS_SANDBOX_CLIENT_ID` / `UBER_EATS_SANDBOX_CLIENT_SECRET` | **Testing** app (the "NextGen Intelligence POS TEST" application). |
| `UBER_EATS_SANDBOX_API_BASE_URL` / `UBER_EATS_SANDBOX_AUTH_BASE_URL` | Defaults: `https://test-api.uber.com`, `https://sandbox-login.uber.com`. Uber's Getting Started page says `sandbox-auth.uber.com`; its Sandbox guide (which has the worked example) says `sandbox-login`. Override this if Uber's team says otherwise. |
| `UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS` | Comma-separated business UUIDs that may turn **sandbox** orders into real Orders. Only a dedicated test business belongs here, never a live restaurant. Empty means every sandbox order is denied. |
| `UBER_EATS_SANDBOX_ONLY_DEPLOYMENT` | Default `false`. Set `true` only on a deployment that deliberately holds the sandbox app alone, such as sandbox testing on the single production deployment before Uber issues production credentials. While it is on, no connection can be promoted to `CONNECTED`. |
| `DELIVERY_ACCEPT_DEADLINE_SECONDS` | Default 600. With the 30s grace and one 30s watchdog tick this stays under Uber's 690s auto-cancel. Scheduled orders are capped tighter, at Uber's 10-minute window. |
| `DELIVERY_WEBHOOK_RATE_LIMIT_PER_MINUTE` | Default 120 per client IP. **All** tenants' Uber webhooks share Uber's sender IPs, so raise this before onboarding many stores. |

Boot guard: with webhooks on, a production deployment needs the **production** pair. The only exception is `UBER_EATS_SANDBOX_ONLY_DEPLOYMENT=true` with a complete sandbox pair and no production values; a warning is logged at boot. A missing variable never implies sandbox-only, because that would silently 200-ack every genuine production order as `BAD_SIGNATURE` (BUG-0089). A half-configured pair in either environment is fatal.

Environment isolation: an event signed with one app's secret is ignored (`ENV_MISMATCH`) when it names a store registered for the other environment. That covers every event type, cancellations included, and is checked before any order row or job exists. Sandbox event ids are deduplicated in their own namespace.

## 4. Sandbox onboarding checklist

1. In the Uber developer dashboard, on the TEST app:
   - set the webhook URL to `https://<backend-host>/v1/webhook/delivery/UBER_EATS`;
   - ask Integration Tech Support for a **test store** and note its store id.
2. Create a dedicated test business in NextGen, e.g. "NextGen Uber Sandbox". Never use a real restaurant's tenant.
3. In the Admin Suite (SUPER_ADMIN), enable `delivery_platforms_enabled` and `uber_eats_enabled` on that business.
4. Local testing: keep the sandbox pair in a git-ignored `.env.*` file such as `.env.uber-sandbox` in the backend root (every `.env.*` except the `*.example` templates is ignored). The app itself auto-loads only `.env.backend`, so a local server needs these as real environment variables. On a deployment, set `UBER_EATS_SANDBOX_CLIENT_ID`, `UBER_EATS_SANDBOX_CLIENT_SECRET`, `UBER_EATS_SANDBOX_ORDER_BUSINESS_IDS=<test business id>`, `UBER_EATS_WEBHOOK_ENABLED=true` and, while no production app exists, `UBER_EATS_SANDBOX_ONLY_DEPLOYMENT=true`. Then redeploy the backend **and** the worker.
5. Register the connection (SUPER_ADMIN, admin API):
   `PUT /v1/admin/admin/businesses/{id}/delivery-connections/UBER_EATS`
   with `{"status": "SANDBOX", "environment": "sandbox", "external_store_id": "<test store id>"}`
6. As the test business's manager, open Delivery → Menu Mapping → **Import Uber Eats menu**, then map anything left unmapped.
7. Place sandbox orders and run the pilot checks in §6.

Activation note: Uber's `POST /v1/eats/stores/{id}/pos_data` (`is_order_manager`) needs a merchant-authorised `eats.pos_provisioning` token. That OAuth flow is **not built**. Ask Uber's integration team whether the test store comes pre-activated for the app; if not, the provisioning flow is the next build item.

## 5. Production approval checklist (Uber)

1. Finish sandbox verification (§6) and request **integration verification** through Uber Tech Support.
2. Create a separate **production** app; Uber whitelists its scopes. Request only `eats.order`, `eats.store`, `eats.store.orders.read`, `eats.store.status.write` and `eats.pos_provisioning`. Do not request `eats.report` until settlement reconciliation is built.
3. Set the production webhook URL, and set `UBER_EATS_CLIENT_ID` / `UBER_EATS_CLIENT_SECRET`.
4. Pilot store (The Village Co Cafe & Bistro): NextGen must be the store's **nominated order manager**. Finish menu mapping **before** go-live, because every auto-deny counts against the store.
5. Register the connection as `AWAITING_APPROVAL` (`environment: production`, real store id).
6. Uber's bar: the pilot needs **≥98% injection success for 3 days**; the ongoing target is **99.9%**; API access can be revoked below 99%. The Delivery Platforms page shows the 24h acceptance rate against this.
7. Only after Uber's **written** approval, set `CONNECTED` with `uber_approval_reference` (email or ticket id). The audit row keeps it.

Open questions for Uber's integration team:

- **API version.** Which order API version does certification expect? This build uses the documented v1/v2 Eats endpoints, now labelled "Previous Version", plus the newer `/v1/delivery/order/{id}/ready`, which has no older equivalent.
- **Store prep time.** Uber lists "Update Store Prep Time" as required for certification. It is **not built**.
- **Activation.** Is the test store pre-activated, or does it need `pos_data` (see §4)?

## 6. Pilot verification (sandbox, then the pilot store)

Run each check and record the result. Nothing below has been run against Uber yet.

| # | Check | Expected |
|---|---|---|
| 1 | Send one sandbox order | `delivery_webhook_events` row with `error_category=OK`, `environment=sandbox`; a `delivery_orders` row in `READY_TO_ACCEPT`; the order appears in the Staff PWA "Delivery orders waiting" list with a countdown |
| 2 | Accept it in the Staff PWA | Uber shows it accepted; it appears in Live Orders as `UE-<code>`; one stock deduction per recipe ingredient |
| 3 | Mark it READY in the Staff PWA | Uber shows it ready for pickup; `last_provider_sync_error` empty |
| 4 | Send an order with an unmapped item | `NEEDS_MAPPING`; staff see "a manager must match…"; mapping it then accepting works |
| 5 | Leave an order untouched | Auto-denied about 10–11 minutes in (`deny_reason_code=OTHER`); Uber shows it denied, not timed out |
| 6 | Cancel an accepted order in Uber | NextGen order CANCELLED; kitchen gets a push; stock restored only if it was never started |
| 7 | Replay the same webhook | Exactly one order and one stock deduction |
| 8 | Pause, then resume the store | Uber's store status follows |
| 9 | Import the CSV report covering those orders | Live-API orders reported as duplicates (`duplicate_source=LIVE_API`) and not imported again |
| 10 | Compare totals with Uber's payout statement | `subtotal`/`fees`/`total` mapping holds for a South African store (see §2) |

Real-Postgres coverage of 1, 2, 4, 6, 7 and 9 (network mocked) is in `tests/integration/test_delivery_marketplace_realdb.py`.

## 7. Mr D

- No public partner API exists, and NextGen holds no credentials. `MrDAdapter` makes no network calls and is not webhook-enabled.
- The admin API refuses any Mr D status other than `NOT_CONNECTED`/`DISABLED`. The dashboard badge reads **"Pending partner approval"**.
- Mr D sales are reconciled by importing Mr D's CSV export. Item and modifier mapping, VAT inside the price, idempotent per order id, and the closed-stocktake-period policy all apply.
- When Mr D grants access, implement `MrDAdapter` against **their** specification. Add `MR_D` to `registry._WEBHOOK_ENABLED_PROVIDERS` and `external_orders/service._PLATFORM_TO_LIVE_PROVIDER` in the same change, with a DECISIONS.md entry. Never scrape or reverse-engineer.

## 8. Operating cost, per restaurant per month (incremental)

| Area | Estimate | Basis |
|---|---|---|
| Hosting | **R0 incremental** while the existing Render web, worker, Postgres and Redis plans have headroom | No new service. Each order means about 2 webhook requests, 1 order fetch, 1 accept and 1 ready call. Storage is roughly 5–15 KB per order across `delivery_orders` and `delivery_webhook_events`; payloads are redacted at 30 days and rows deleted at 400 days (retention sweep). At 50 orders/day that is under 25 MB/month. The 30-second watchdog runs one indexed query regardless of tenant count. *Derived; Render prices not re-checked.* |
| Provider charges | Uber's marketplace commission on each order (a restaurant–Uber commercial term, not a NextGen cost). API access fee: **Unknown** (check the signed API licensing agreement). Mr D: Unknown. | — |
| Processing | **R0**: structured orders use no LLM. Token mints happen about once per 30 days per environment. Web push and SSE use the existing services. | Verified by code search 2026-10-26 |

## 9. Rollback

- **Code.** Revert the merge commit. Migration `0059_delivery_kitchen_flow` has a tested downgrade (upgrade → downgrade → upgrade was run on a disposable Postgres). Downgrading maps any `ACCEPT_UNCONFIRMED` row to `FAILED`, so the watchdog still never denies it.
- **Operations.** Set `UBER_EATS_WEBHOOK_ENABLED=false` and redeploy to stop all Uber processing, or disable one store's connection from the dashboard or admin API.
