# Souk release checklist (backend 0059–0071 + three frontends)

Status 2026-11-19: **not released.** Every step below needs the owner's go-ahead. Claude does not merge, deploy or migrate production.

Merging the backend to `main` makes Render run `alembic upgrade head` on boot (`start.sh`). That is what migrates production. Read `docs/MIGRATION_RECONCILIATION_2026-11-18.md` §6–§7 first.

## 0. Gate: all must be true before step 1

- [ ] Owner actions done (see `OWNER_ACTIONS_2026-11-19.md` in the v9 workspace): BUG-0184 credentials rotated, demo super admin checked, repo visibility decided.
- [ ] Owner approved the release and the merge order in writing.
- [ ] On the exact merge commit:
  - [ ] `alembic heads` shows only `0071_event_wa_ready_template`;
  - [ ] full `pytest tests/` and `tests/integration` green;
  - [ ] `tests/replay` at its baseline;
  - [ ] each frontend: `npm run typecheck` and `npx vitest run` green.
- [ ] Migration rehearsal repeated on that commit against a restored copy of a recent production backup.
- [ ] Production row count of `orders` known, so the migration time can be estimated (rehearsal: about 1 min for 48k orders on a loaded laptop).
- [ ] Real-device acceptance tests (owner actions §G) passed on staging, or explicitly waived for a controlled pilot.
- [ ] H-3 decision recorded. The commit "fix(bot)!: RESTAURANT-AFFECTING H-3" (and its follow-ups) changes restaurant delivery behaviour. Keep it, or revert it before merging.

## 1. Production environment, set BEFORE the deploy

| Variable | Value | Why |
|---|---|---|
| `UBER_EATS_WEBHOOK_ENABLED` | `false` (the default) | Uber stays off until the production signing key is set |
| `NEXTGEN_BANK_NAME`, `NEXTGEN_BANK_ACCOUNT_NAME`, `NEXTGEN_BANK_ACCOUNT_NUMBER`, `NEXTGEN_BANK_BRANCH_CODE`, `NEXTGEN_BANK_ACCOUNT_TYPE` | Owner's real bank details | Shown to vendors for EFT. Empty means vendors get no account to pay into |
| `EMAIL_BACKEND` | **Blocker:** the default `console` only logs. Production email delivery is not approved or wired | Self-registered vendors cannot verify their email, so they cannot trade. Until delivery exists, onboard pilot vendors through the super-admin bulk import: admin-provisioned vendors don't need verification |
| `EMAIL_VERIFICATION_LINK_BASE_URL` | The production manager dashboard URL | The link in the verification email |
| `EMAIL_FROM_ADDRESS` | A verified sender, once delivery is approved | |

The variables are defined in `backend/app/core/config.py`. Vendor email details are in `docs/VENDOR_EMAIL_VERIFICATION.md`.

## 2. Order

1. **Backup:** take a production DB snapshot (Supabase → Database → Backups) and note the time.
2. **Backend merge:** merge steps 1–4 of the reconciliation doc, one at a time. Watch the Render log for each `alembic upgrade` line and for `Application startup complete`.
3. **Worker:** redeploy the worker after the web service. Its new jobs are:
   - abandoned-registration cleanup;
   - event reconcile;
   - worker log masking (H-2).

   Check the worker log for `arq` startup and that no phone number appears unmasked.
4. **Frontends:** deploy admin, then manager, then staff. Each one copes with an older backend for the new fields, but not the reverse, so the backend goes first.
5. **Staff app on tills:** each till picks up the new version on its next load. Ask restaurants to reload once. The POS ready-message consent tick-box only appears after the reload; see `docs/POS_ORDER_READY_CONSENT.md`.

## 3. Smoke test after each step (5 minutes)

**Restaurant (pick one live tenant):**
- [ ] Manager login. Orders list loads. Menu list loads, and items carry `has_variants`.
- [ ] Staff till: one cash sale, then mark it ready, then collected. No WhatsApp is sent unless the consent box was ticked.
- [ ] A WhatsApp test order on the controlled test number, if available. It flows exactly as before.

**Souk (a test event created by the super admin):**
- [ ] Create the event, publish its days, bulk-onboard 2 test vendors.
- [ ] Before payment, the vendor can configure but a POS sale is refused (`EVENT_TRADING_NOT_ALLOWED`).
- [ ] Record payment for today, then the sale works.
- [ ] Suspend the vendor: the vendor is logged out. Reinstate.

## 4. Kill switches and rollback

| Problem | Action |
|---|---|
| WhatsApp misbehaves for one vendor | Super admin sets that vendor's `is_whatsapp_enabled=false`. The bot then processes nothing for that event vendor |
| A souk vendor must stop trading at once | Super admin suspends the vendor (`EVENT_ADMIN_SUSPENDED`). An admin suspension never gets the sync grace |
| Code regression | Roll the code back on Render (deploy the previous commit). Code rollback without a schema rollback is safe for 0062–0071 |
| Schema rollback | Last resort only. Data loss per reconciliation doc §7: event data, variants, pending late uploads, verification state |

## 5. Not covered by this release (known)

- **Production email delivery:** not approved.
- **WhatsApp for event vendors:** requires, per vendor:
  - the vendor's own number connected through Embedded Signup;
  - a payment method on their WABA;
  - an approved `order_ready_v1` template in their WhatsApp Manager;
  - a super admin enabling it.

  See `docs/EVENT_WHATSAPP.md`.
- **Open findings:** BUG-0212 M-1/M-2/M-3 and the Lows marked Open in `KNOWN_BUGS.md`.
- **Devices:** real-device offline behaviour, especially iOS storage eviction of queued sales.
