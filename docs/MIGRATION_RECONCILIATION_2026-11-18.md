# Migration-chain reconciliation and merge strategy (2026-11-18)

**Status**: prepared and verified on disposable databases only. **Nothing has been merged to `main` or applied to production.** Draft PRs: backend #10 and #11 (2026-11-19).

**Current chain head: `0071_event_wa_ready_template`.** See §7 (2026-11-19) for the full chain from production `0058`, the production-shaped rehearsal, and the rollback limits of every new migration. §4–§6 below are the 2026-11-18 plan to `0062` and still describe steps 1–3.

## 1. Why this matters
`start.sh` runs `alembic upgrade head` on every boot, and Render deploys `main`. So **merging to `main` is what migrates production**. A graph with two heads makes `upgrade head` fail, and the backend would not start.

## 2. Production head (not modified)
**`0058_business_menu_images`** (`Derived`; no direct `alembic_version` read was possible without database access). Evidence:

1. `origin/main` (`0b3bfdc`) ends at `0058`.
2. The production hotfix release `e65ec31` ends at `0058`, and it is in `main` (PR #8).
3. The live API serves `0058`'s menu-images route (401) but not `0059_delivery_kitchen_flow`'s delivery queue (404). Read-only GET probes, 2026-11-18.

## 3. Conflicting migrations found

| Revision | Branch | Parent | Applied where |
|---|---|---|---|
| `0058_business_menu_images` | `main` | `0057_pos_entitlement` | **Production** |
| `0059_delivery_kitchen_flow` | `feat/marketplace-integrations` → `feat/sales-wallets` | `0058_business_menu_images` | Local Uber sandbox DB and disposable test DBs |
| `0059_whatsapp_routing_generation` | `fix/whatsapp-routing-generation-binding` | `0058_business_menu_images` | Disposable test DBs only |
| `0058_receipt_status_widen` | `fix/whatsapp-receipt-text-fallback-demo` (local, never pushed, 47 commits behind) | `0057_pos_entitlement` | Disposable test DBs only |

Dependencies: none between them. They touch different tables (`delivery_orders`; `businesses.whatsapp_routing_generation`; `digital_receipts`), so their order does not change the resulting schema.

## 4. Resolved graph (single head)

```
0057_pos_entitlement
  -> 0058_business_menu_images           (production; unchanged)
  -> 0059_delivery_kitchen_flow          (unchanged id)
  -> 0060_whatsapp_routing_generation    (renumbered from 0059_whatsapp_routing_generation)
  -> 0061_business_user_token_version    (new: security/access-token-version)
  -> 0062_events_foundation              (new: feat/souk-event-foundation)
```

- **Why no alembic merge revision**: `tests/test_migration_chain_single_head.py` also forbids two files sharing a number. A merge revision would leave two `0059`s. Renumbering the routing revision is safe because it was never applied to production.
- **Why the delivery migration keeps `0059`**: it is applied in the local Uber sandbox database that holds the authentic test orders. Keeping its id means that database upgrades in place.
- **The receipt branch**: not merged now. When it is, its migration must be renumbered to follow the then-current head (its parent becomes that head). It has never been applied outside disposable databases, so renaming is safe. Any disposable DB stamped with an old renamed id must be recreated (`docker compose -f docker-compose.test.yml down -v`).

## 5. Verification (isolated container `nextgen-reconcile-pg`, Postgres 16; the shared test DB and the Uber sandbox were not touched)

| Test | Result |
|---|---|
| Fresh DB: base → head → `0058` → head → base → head | Pass |
| Production-shaped copy (the 0058 test DB's 29 businesses, 8 orders and 8 users, plus delivery orders in all 10 statuses): upgrade to head | Pass. Every pre-existing row byte-identical (checksums over the 0058 columns); `whatsapp_routing_generation`=1 for every business; `ACCEPT_UNCONFIRMED` accepted |
| Same copy: rollback to `0058` | Pass. Original rows byte-identical. **Limitation**: an `ACCEPT_UNCONFIRMED` delivery order becomes `FAILED` (0059's designed downgrade) |
| `0061` on fresh and copy DBs: up/down/up | Pass. All existing users get `token_version=0`, so the deploy logs no one out |
| `0062` on a fresh DB: up/down/up | Pass |
| Backend `tests/` on the integration branch | **4051 passed, 0 failed, 90 skipped** |
| `tests/integration` on the integration branch (isolated DB at `0060`) | **278 passed, 0 failed** (includes the marketplace and routing real-DB tests) |

### Rollback limitations
- Downgrading `0059` turns `ACCEPT_UNCONFIRMED` into `FAILED` and drops the provider display id and sync-trail columns.
- Downgrading `0060` drops every business's routing generation. Re-upgrading resets it to 1. **Drain the worker queue before rolling back**: a queued WhatsApp job carrying an older snapshot would otherwise be refused (fail-closed).
- Downgrading `0061` only loses the instant access-token cut-off.
- Downgrading `0062` drops all event data.
- A **code** rollback without a schema rollback is safe for `0060`–`0062` (additive columns with defaults). After `0059`, older code may not recognise `ACCEPT_UNCONFIRMED` rows.

## 6. Merge strategy (for review; each step needs the owner's go-ahead)

1. **PR #10** (`security/bug-0184-remove-hardcoded-credentials`): no migration. Can merge first, independently.
2. **Integration branch** `integration/migration-reconcile-20261118`: `main` + routing fix + `feat/sales-wallets` (the Uber Eats marketplace, Sales Wallets, rankings and go-live fixes), plus the renumber. Push and open a PR, then merge. Production then migrates `0058 → 0060` on boot.
   - Before merging: set `UBER_EATS_WEBHOOK_ENABLED=false` in production. It keeps all Uber processing off until the production signing key is configured.
   - Redeploy the worker after the web service.
3. **`security/access-token-version`** (`0061`): stacked on step 2 and includes PR #10. Merge after step 2.
4. **Souk release** (`0062`–`0071`): stacked on step 3. Merge from `integration/souk-pilot`, or from `feat/souk-event-foundation` once that branch has been fast-forwarded to it. See §7 and `docs/SOUK_RELEASE_CHECKLIST.md`.
5. The frontends (manager and staff `feat/marketplace-integrations` / `feat/sales-wallets`) deploy after step 2. Both handle the backend features being absent.

**Pre-merge checklist**:
- `alembic heads` shows exactly one head.
- `tests/test_migration_chain_single_head.py` passes.
- Full `tests/` and `tests/integration` pass on the exact merge commit.
- Take a production DB backup or snapshot immediately before the step-2 deploy.

## 7. Update 2026-11-19: chain to `0071` and production-shaped rehearsal

### Chain (single head; `tests/test_migration_chain_single_head.py` passes)
```
0058_business_menu_images           (production)
  -> 0059_delivery_kitchen_flow
  -> 0060_whatsapp_routing_generation
  -> 0061_business_user_token_version
  -> 0062_events_foundation
  -> 0063_event_access_levels
  -> 0064_event_self_registration
  -> 0065_order_notify_when_ready
  -> 0066_product_variants
  -> 0067_offline_pos_capture
  -> 0068_offline_review_hardening
  -> 0069_offline_capture_review        (feat/souk-sync-grace)
  -> 0070_vendor_email_verification     (feat/souk-email-verification; re-pointed from 0068 at merge)
  -> 0071_event_wa_ready_template       (feat/souk-whatsapp-gate; re-pointed from 0068 at merge)
```
`0069`–`0071` were written in parallel against `0068`. They were chained at the merge into `integration/souk-pilot` (`bc39a2b`). They touch different tables and columns, so their order does not change the resulting schema.

### Rehearsal (`nextgen-reconcile-pg`, database `rehearsal_prodshape`; production was not touched)

**Source data:** a clone of `deployed_copy`, the same 0058 production-shaped copy used in §5: 29 businesses, 83 menu items, 8 users. It was bulked up by replicating existing rows with fresh ids to **48,008 orders, 60,010 order items and 1,818 customers**. The rehearsal script lives in the session scratchpad, not in the repo.

| Step | Result | Time* |
|---|---|---|
| `alembic upgrade head` (0058 → 0071) | Pass. All 52 pre-existing tables are byte-identical over their 0058 columns (row count plus md5 per table). 7 new tables; new columns on `businesses`, `business_users`, `orders`, `order_items`, `delivery_orders` | 63.2 s |
| `alembic downgrade 0058_business_menu_images` | Pass. Every pre-existing table byte-identical; no leftover table or column | 42.5 s |
| `alembic upgrade head` again | Pass | 57.6 s |

\*The machine was at or near 100% CPU from parallel test runs, so these times are an upper bound for this data size. **Production row counts are Unknown.** Before the release, check `select count(*) from orders` in production (read-only) and allow for the `orders` ALTERs at that size. Every new column is nullable or has a constant default, and Postgres 11+ adds such columns without rewriting the table.

### Rollback limitations, 0062–0071 (in addition to §5)
- **0062–0064:** downgrade drops all event, vendor, subscription and self-registration data.
- **0065:** drops `orders.notify_when_ready` (the consent record). It does **not** restore `is_whatsapp_enabled` for event-plan businesses.
- **0066:** drops `product_variants` and the variant snapshot columns on `order_items`. Order history keeps line totals but loses the size/colour labels.
- **0067/0068:** drops the offline-capture columns on `orders`. Offline sales stay as ordinary orders, and their capture time and review flags are lost.
- **0069:** drops `offline_capture_reviews`. **Pending late uploads are lost.** Decide them first, or export the table.
- **0070:** drops the email-verification columns and tokens. Older code ignores verification, so unverified vendors would no longer be capped at CONFIGURE.
- **0071:** drops the per-vendor order-ready template config and consent provenance.
- **Code rollback without a schema rollback:** safe for 0062–0071 (additive, defaults). Older code ignores the new tables and columns.

### Pre-merge checklist additions
- `alembic heads` shows exactly `0071_event_wa_ready_template`.
- Repeat this rehearsal on the exact merge commit, ideally on a restored copy of a recent production backup, never on production itself.
- Full `tests/` and `tests/integration` on the exact merge commit.
