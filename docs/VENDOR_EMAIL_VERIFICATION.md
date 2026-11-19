# Vendor email verification (souk self-registration)

Status: implemented on branch `feat/souk-email-verification` (local, not merged, not deployed).
Production email delivery is **not approved**: only local mock email backends exist and no real
provider is wired.

## Who needs to verify

Only an owner created by `POST /v1/public/events/{slug}/register` (vendor self-registration).
Their row has `business_users.email_verification_required = true`.

Everyone else keeps the column default `false` and is never affected: restaurants, businesses
created by a super admin, souk vendors bulk-onboarded by a super admin, managers and staff.

## What an unverified vendor can and cannot do

| Action | Unverified self-registered vendor |
|---|---|
| Log in (email + password) | Allowed (so they can set up their stall) |
| Configure products, stock, staff, images | Allowed (access level CONFIGURE) |
| Quote event days (`POST /business/event-subscriptions`) | Allowed |
| Claim an EFT payment (`.../payment-claim`) | Refused: `403 EMAIL_NOT_VERIFIED` |
| Trade (POS sale, orders, payment intents, offline sync, anything behind `assert_trading_allowed`) | Refused: `403 EVENT_TRADING_NOT_ALLOWED`, `details.reason = EMAIL_NOT_VERIFIED` |

The access decision (`backend/app/events/service.py::business_access_decision`) is computed live
on every call. When the event rules would give TRADE and the business has an owner whose required
verification is not done, the decision becomes `CONFIGURE / EMAIL_NOT_VERIFIED`. NONE and other
CONFIGURE reasons are left unchanged (they are the more fundamental answer).
`GET /v1/business/event-access` also returns
`email_verification: {"required": bool, "verified": bool}` so the dashboard can show a banner and a
resend button.

## Endpoints

| Method / path | Body | Answer |
|---|---|---|
| `POST /v1/public/events/{slug}/register` | unchanged | `202` `{"status": "verification_pending", "next": "check_email", "message", "access_level", "login_opens_on"}` for every registration that passes the code check, new email or not |
| `POST /v1/public/auth/verify-email` | `{"token"}` | `200` `{"verified": true, "next": "login", "business_code"}`; any unknown, expired, used or superseded token: `400 VERIFICATION_TOKEN_INVALID` (one message) |
| `POST /v1/public/auth/resend-verification` | `{"email"}` | always `202` `{"status": "accepted", "message"}` |

Rate limits (per client IP, slowapi): verify `EMAIL_VERIFY_RATE_LIMIT_PER_MINUTE` (10/min);
resend `EMAIL_RESEND_RATE_LIMIT_PER_MINUTE` (3/min) and `EMAIL_RESEND_RATE_LIMIT_PER_HOUR`
(20/hour); register keeps its per-IP limits and the per-event hourly budget
(`EVENT_REGISTER_RATE_LIMIT_PER_EVENT_PER_HOUR`).

Per email, silently (the answer is still 202): a 60 s cooldown and at most 5 verification emails
per hour per account (counted in Postgres from the account's own token rows), plus a
10-per-hour budget per SHA-256 of the email in Redis that applies to any email, known or not
(fails open to the Postgres cap if Redis is unavailable).

## Tokens

- 32 random bytes (`secrets.token_urlsafe(32)`). Only `sha256(token)` is stored
  (`email_verification_tokens.token_hash`).
- Valid 24 hours (`TOKEN_TTL`).
- Single use: one conditional `UPDATE ... SET used_at = now WHERE token_hash = :h AND used_at IS NULL
  AND invalidated_at IS NULL AND expires_at > now RETURNING user_id`. Two concurrent requests with
  the same token serialise on the row lock; exactly one succeeds (tested on real Postgres).
- A new token (resend) sets `invalidated_at` on all earlier unused tokens of that user; a
  successful verification invalidates the rest.
- Bound to one user: verifying only writes `email_verified_at` on that token's own user, and only
  if the user is active and not closed as abandoned.
- The link is `{EMAIL_VERIFICATION_LINK_BASE_URL}/verify-email#token=...`. The token is in the URL
  **fragment**, which browsers never send to a server, so it does not land in access logs or
  Referer headers. The dashboard page removes it from the address bar immediately.
- No raw token is ever logged or written to the audit log. The console mock logs only the
  recipient's domain and the subject.

## Enumeration

- Register: a duplicate email gets exactly the same 202 body as a new registration, creates
  nothing, and the existing address gets a "someone tried to register with your email" notice
  (at most 3 per account per hour). The password is bcrypt-hashed on every path so timing
  matches. The business code is no longer in the register response; it is in the verification
  email and in the verify-email response. Owner login is email + password and never needed it.
- Resend: identical 202 for unknown, verified, closed, throttled and real resends. The not-eligible
  path runs the same lookup and count queries (against a placeholder id). Known limit: a real
  resend additionally does one INSERT, one commit and the (mock) send, so it is measurably slower
  than a no-op by a few milliseconds; with a real provider the send should move to the outbox
  (see open items) to keep that difference small.
- Verify: invalid / expired / used / superseded tokens return one code and one message.

## Abandoned registrations

Hourly worker job `close_abandoned_vendor_registrations` (`backend/app/worker.py`, minute 10)
calls `services/email_verification.py::close_abandoned_registrations`. It soft-closes every
self-registered owner still unverified 7 days (`ABANDON_AFTER`) after creation whose business has
**no PAID event subscription**:

- owner: `registration_abandoned_at` set, `is_active = false`, sessions ended
  (token_version bump, refresh tokens revoked), outstanding verification tokens invalidated;
- business: `is_active = false`, `suspended_reason = EMAIL_VERIFICATION_ABANDONED` (only when it
  was active or carried the event job's own `EVENT_SUBSCRIPTION_ENDED`; an administrator's
  suspension is left as it is). The event reconcile never lifts this reason;
- UNPAID subscriptions voided, the business's EventVendor rows set CANCELLED;
- one audit row `auth.registration_abandoned_closed` (counts only, no PII).

Nothing is deleted. Rows are locked `FOR UPDATE SKIP LOCKED`, so a busy row waits for the next run.

### Re-registering with the same email

If the only account holding the email is an owner that was self-registered, never verified AND
already closed as abandoned, a new registration releases that address in the same transaction
(the old owner's `email` is set to NULL, audit `auth.abandoned_registration_email_released`) and
creates a fresh account, which must verify again. Any other holder (a verified account, an
unverified one inside its 7 days, an admin-provisioned or bulk-onboarded owner, a restaurant
owner, a manager) goes down the duplicate path: same 202, nothing created, nothing changed.

## Audit events

`auth.email_verification_sent`, `auth.email_verified`, `auth.duplicate_registration_notice_sent`,
`auth.duplicate_registration_attempt` (notice throttled), `auth.registration_abandoned_closed`,
`auth.abandoned_registration_email_released`, plus the existing `event.vendor_self_registered`.
None contains an email address, password, registration code or token.

## Schema (migration `0070_vendor_email_verification`)

- `business_users.email_verification_required boolean NOT NULL DEFAULT false`
- `business_users.email_verified_at timestamptz NULL`
- `business_users.registration_abandoned_at timestamptz NULL`
- `email_verification_tokens (id, user_id FK business_users ON DELETE CASCADE, token_hash
  varchar(64) UNIQUE, expires_at, used_at, invalidated_at, created_at)`, index on `user_id`.

`down_revision` is `0068_offline_review_hardening` on this branch; re-point it at
`0069_offline_capture_review` when the parallel branch merges.

## Configuration (names only)

Already usable locally:

- `EMAIL_BACKEND`: `console` (default; logs domain + subject only), `memory` (tests), `file`
  (writes `.eml` files; refused outside ENVIRONMENT development/test).
- `EMAIL_FILE_DIR`: directory for the `file` backend.
- `EMAIL_FROM_ADDRESS`: sender address used in messages.
- `EMAIL_VERIFICATION_LINK_BASE_URL`: base URL of the manager dashboard (the link is
  `{base}/verify-email#token=...`).
- `EMAIL_VERIFY_RATE_LIMIT_PER_MINUTE`, `EMAIL_RESEND_RATE_LIMIT_PER_MINUTE`,
  `EMAIL_RESEND_RATE_LIMIT_PER_HOUR`.

Needed before production email delivery is approved (not implemented; owner decision):

- a provider choice and its credentials (for example a provider API key variable, to be named
  when the provider is chosen) plus a new `EmailSender` implementation in
  `backend/app/email/sender.py` selected by a new `EMAIL_BACKEND` value;
- a verified sending domain (SPF, DKIM, DMARC) matching `EMAIL_FROM_ADDRESS`;
- `EMAIL_VERIFICATION_LINK_BASE_URL` set to the production dashboard URL.

**Until a real provider exists, self-registered vendors cannot receive their link in production
and therefore cannot trade.** With `EMAIL_BACKEND=console` in production the message is only
logged (without the link). Do not enable public self-registration in production before this is
resolved, or verify vendors manually (no admin "mark verified" action exists yet; see open items).

## Open items

- Real email provider (owner approval needed), ideally sending through the existing message outbox
  so a provider outage does not lose links and request timing does not depend on the provider.
- No super-admin action to mark an owner verified or resend on their behalf.
- An abandoned vendor's staff accounts stay active rows but cannot log in because the business is
  inactive (login checks the business).
- An email change flow for owners does not exist; when one is added it must reset
  `email_verified_at` for self-registered owners.
