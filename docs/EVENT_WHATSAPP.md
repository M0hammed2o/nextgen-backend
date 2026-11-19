# WhatsApp for souk event vendors

Status: implemented on branch `feat/souk-whatsapp-gate` (local only, not deployed).
Nothing here has been tried against Meta: every test mocks the Meta API, and no
WhatsApp Business account (WABA) or template was created or changed.

## 1. What the platform enforces

| Rule | Where |
|---|---|
| A WhatsApp customer can only place a real order on one of the vendor's **paid trading days** (access level TRADE, in the event's own timezone). | `pipeline._process` early gate; hard gates at the top of `_handle_order_confirmation` and `_finalize_pending_delivery_order`; a raising backstop in `order_creator.create_order_from_cart` (before any order row, stock movement or live-order alert). |
| On any other day the bot does not call the AI, clears the cart, cancels a delivery order still waiting for its fee (stock is put back), and sends one neutral notice per event-local day. | `pipeline._event_vendor_soft_gate`, `_event_refusal_cleanup` |
| WhatsApp is off for an event vendor until a **super admin** switches it on for that one vendor (`is_whatsapp_enabled`, the per-vendor kill switch). While off, inbound messages are recorded but nothing is processed or sent. | admin PATCH `/admin/businesses/{id}`; pipeline soft gate |
| An event vendor messages customers only from **its own** WhatsApp Business number (Embedded Signup). It never gets NextGen's platform token, so Meta bills the vendor, not NextGen. | `whatsapp/token_resolver.py` |
| Nobody can type a WhatsApp number or WABA id for an event vendor. The number only comes from the vendor's own Embedded Signup. | admin PATCH (422 `EVENT_PLAN_WHATSAPP_NOT_ALLOWED`) |
| The vendor can always disconnect its own number, even while WhatsApp is switched off. | `DELETE /v1/business/whatsapp/connection` |
| Accepting an online-payment order (which creates a payment link) and quoting a delivery fee need a paid trading day. | `routes_orders.update_order_status`, `set_delivery_fee` |
| A provider payment that arrives on a non-trading day is still recorded (the money has moved) and flagged for the organiser: an order event plus audit event `event.payment.outside_trading_day`. | `routes_payments._mark_order_paid` |
| Restaurants are unaffected: every check returns on the business plan the code already has, before any database query. | tests prove it (see §6) |

## 2. How an event vendor goes live on WhatsApp

1. **Vendor asks NextGen to enable WhatsApp.** A super admin sets
   `is_whatsapp_enabled = true` for that vendor only (admin PATCH). There is no
   bulk switch. Until then the manager app shows "Not connected: awaiting
   NextGen approval".
2. **Vendor connects its own number** in the manager app: Event home, WhatsApp
   card, "Connect WhatsApp" (the existing Embedded Signup page,
   `/settings/whatsapp`). The vendor signs in to Meta, picks or creates its own
   WhatsApp Business account and phone number.
3. **Vendor adds a payment method to its WhatsApp Business account** in Meta
   Business Suite (WhatsApp Manager, Payment settings). Meta bills the vendor
   directly for conversations; NextGen does not pay or resell these charges.
4. **Vendor submits the order-ready template** in its own WhatsApp Manager
   (Message templates, Create template). NextGen does not create templates
   through the API.
   - Category: **Utility**
   - Name: `order_ready_v1`
   - Language: English (US), `en_US`
   - Body: `Hi! Your order {{1}} from {{2}} is ready for collection at {{3}}.`
   - Sample values: `{{1}}` = `A-0042` (order number), `{{2}}` = the stall's
     trading name, `{{3}}` = `stall F-12, Durban Summer Souk`
   - No header, no buttons, no links.
5. **Super admin confirms the template.** After seeing the template as
   APPROVED in the vendor's WhatsApp Manager (screen share or screenshot), the
   super admin sets `whatsapp_ready_template_name`,
   `whatsapp_ready_template_language` and `whatsapp_ready_template_approved =
   true` (admin PATCH; ONBOARDER cannot). Renaming the template later clears the
   approval automatically. The audit log records who approved it and when.
6. The manager app now shows "Ready", and the staff till offers the
   "Notify customer when ready (WhatsApp)" tick-box for this vendor.

## 3. The order-ready message

- Only for a till (POS) order where the cashier ticked the customer's consent;
  who recorded it and when is stored (`orders.notify_consent_at`,
  `orders.notify_consent_by_user_id`).
- Never to a customer who has opted out (STOP).
- At most one message per order.
- With an approved template: sent through the vendor's own connection.
- Without one: plain text only if the customer messaged the vendor in the
  last 24 hours (Meta's customer-service window); otherwise nothing is sent and
  the order shows "Order-ready WhatsApp not sent: no approved template".
- Every outcome is written on the order as an order event.
- Restaurants keep their existing plain-text READY message. Option for later:
  restaurants could use the same template path when one is configured, which
  would not change anything for a restaurant without a template. Not done in
  this change.

## 4. What customers see on a non-trading day

> Sorry, *{vendor}* isn't taking WhatsApp orders right now. WhatsApp ordering
> is only open on their trading days at {event}. Nothing has been ordered and
> you have not been charged. Please visit the stall, or message again on a
> trading day.

It never says whether the vendor has paid. It is sent at most once per
customer per event-local day; further messages that day get no reply. A
suspended vendor (access NONE, business inactive) gets no reply at all, as
before.

## 5. Billing model

- Conversations are billed by Meta to the WhatsApp Business account that sends
  them. An event vendor always sends from its own account (token resolver
  rule), so the vendor pays Meta.
- NextGen's platform token and WABA are never used for an event vendor.
- The NextGen event day rate is separate and unchanged.

## 6. Tests

- `tests/test_event_whatsapp_gate.py`: mocked unit tests, mutation-checked.
- `tests/integration/test_event_whatsapp_gate_realdb.py`: real Postgres,
  `process_inbound_message` end to end, including a 23:59:59 to 00:00:01
  rollover in `Asia/Tokyo`, stock reversal, and a statement capture showing a
  restaurant issues no event query.
- `tests/integration/test_event_whatsapp_routes_realdb.py`: payment link,
  delivery fee, provider payment flag, connection routes, admin rules,
  order-ready template, event-access readiness, consent provenance.
- `tests/test_token_resolver_event_plan.py`, `tests/test_worker_log_masking.py`,
  `tests/test_h3_delivery_finalise_no_resurrection.py`.

## 7. Unknowns (not settled by this change)

- **Tech Provider vs Solution Partner.** Whether NextGen's Meta app is a Tech
  Provider (each vendor adds its own payment method, as assumed here) or a
  Solution Partner with a credit line (NextGen could be billed). Settle by
  checking the Meta app's partner type and any credit line in Meta Business
  Settings.
- **Embedded Signup permission scope.** Whether the app's Embedded Signup
  configuration grants `whatsapp_business_management` (needed for the vendor's
  template list to be readable) in addition to `whatsapp_business_messaging`.
  This change does not read templates through the API; approval is a manual
  super-admin confirmation.
- **Template approval time and wording.** Meta may reject or recategorise the
  suggested template; only a real submission settles it.
- **Whether customers who never messaged a vendor accept a utility template
  from an unknown stall number.** Only real use will show.
