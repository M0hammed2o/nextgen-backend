"""
Go-live checks — the admin-plane half of onboarding readiness.

`backend.app.api.v1.routes_setup.compute_setup_progress` (reused as-is, see
routes_businesses.py) answers "has the owner configured their restaurant
yet?" in owner-facing language. This module adds the narrower, stricter
question a sales partner and NextGen itself need answered before calling a
tenant handed-over: "will this tenant actually work end to end?" Every
check here is computed from real data — nothing is a manually-ticked flag —
matching routes_setup.py's own stated philosophy.

Deliberately NOT a duplicate of get_recipe_coverage's full per-item,
per-size logic (routes_setup.py ~:506-599): that function is scoped to
`AuthUser`-based owner auth, not `(db, business)`, and the task's file
boundary for this branch only permits extracting routes_setup.py's existing
*progress* computation, not adding new admin-facing logic to that file.
Recipe coverage here is intentionally a simpler count-based signal (items
with zero recipe lines) — good enough to gate go-live, not a replacement
for the richer per-size owner-facing report.
"""

import uuid

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.enums import OrderSource
from shared.models.admin import AdminUser
from shared.models.audit import MessageOutbox
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.menu import MenuCategory, MenuItem
from shared.models.order import Order
from shared.models.recipe import Recipe
from shared.models.stocktake import StocktakeSession
from shared.models.till import TillSession
from shared.models.user import BusinessUser

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_NA = "n/a"


class GoLiveCheck(BaseModel):
    key: str
    label: str
    status: str  # pass | fail | warn | n/a
    detail: str


class ReadinessSummary(BaseModel):
    passed: int
    total: int
    percent: int
    label: str


class GoLiveResult(BaseModel):
    ready: bool
    passed: int
    failed: int
    warned: int
    checks: list[GoLiveCheck]
    readiness: ReadinessSummary


async def compute_go_live_checks(db: AsyncSession, business: Business) -> GoLiveResult:
    business_id = business.id
    checks: list[GoLiveCheck] = []

    # ── Owner login exists and has set a real password ─────────────────────
    owner_result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.business_id == business_id,
            BusinessUser.role == "OWNER",
            BusinessUser.is_active.is_(True),
        )
    )
    owners = owner_result.scalars().all()
    if not owners:
        checks.append(GoLiveCheck(
            key="owner_login", label="Owner login",
            status=STATUS_FAIL, detail="No active OWNER login exists yet",
        ))
    else:
        password_set = any(not o.must_change_password for o in owners)
        checks.append(GoLiveCheck(
            key="owner_login", label="Owner login",
            status=STATUS_PASS if password_set else STATUS_FAIL,
            detail=(
                "Owner has set their own password" if password_set
                else "Owner still has a temporary password — they must sign in and set a real one"
            ),
        ))

    # ── At least one active staff member with a PIN ────────────────────────
    staff_with_pin = int((await db.execute(
        select(func.count()).select_from(BusinessUser).where(
            BusinessUser.business_id == business_id,
            BusinessUser.role == "STAFF",
            BusinessUser.is_active.is_(True),
            BusinessUser.pin_hash.isnot(None),
        )
    )).scalar() or 0)
    checks.append(GoLiveCheck(
        key="staff_pin", label="Staff PIN login",
        status=STATUS_PASS if staff_with_pin else STATUS_WARN,
        detail=(
            f"{staff_with_pin} staff member(s) can clock in at the till" if staff_with_pin
            # PIN login (POST /auth/login/pin, see auth_service.py::login_pin)
            # is STAFF-only by design — an owner/manager cannot log into the
            # staff-ops till PWA at all. Without at least one staff PIN, the
            # till literally cannot be operated by anyone; owners/managers
            # manage the business (including voiding/refunding a till sale)
            # from the Manager Dashboard instead, not the till itself.
            else "No staff member has a PIN yet — the till cannot be operated "
                 "(PIN login is STAFF-only; owners/managers void/refund from "
                 "the Manager Dashboard instead, not the till)"
        ),
    ))

    # ── Menu: at least one active category and item ────────────────────────
    active_categories = int((await db.execute(
        select(func.count()).select_from(MenuCategory).where(
            MenuCategory.business_id == business_id, MenuCategory.is_active.is_(True),
        )
    )).scalar() or 0)
    active_items = int((await db.execute(
        select(func.count()).select_from(MenuItem).where(
            MenuItem.business_id == business_id,
            MenuItem.is_deleted.is_(False),
            MenuItem.is_active.is_(True),
        )
    )).scalar() or 0)
    menu_ok = active_categories > 0 and active_items > 0
    checks.append(GoLiveCheck(
        key="menu", label="Menu",
        status=STATUS_PASS if menu_ok else STATUS_FAIL,
        detail=f"{active_categories} active categor{'y' if active_categories == 1 else 'ies'}, {active_items} active item(s)",
    ))

    # ── POS-dependent checks (only if the POS module is switched on) ───────
    # migration 0057_pos_entitlement: a WhatsApp-only tenant (pos_enabled
    # False) has no till at all, so every check below that assumes one
    # (VAT/receipt settings being the till's own receipt config, a POS
    # payment method, an opened till session, a rung-up POS test order,
    # the receipt-print test, and the cash-up test further below) is
    # structurally inapplicable rather than failing — same STATUS_NA
    # pattern already used for inventory_enabled/is_whatsapp_enabled below.
    # This is what lets an ONBOARDER complete go-live for a WhatsApp-only
    # tenant without needing SUPER_ADMIN's force=true override.
    if business.pos_enabled:
        # ── VAT / receipt settings ──────────────────────────────────────────
        # Real-browser fix (Wave 0A): this used to pass for a completely
        # untouched tenant, because BOTH of its old signals are true by default
        # on a freshly created business — vat_registered defaults False (so
        # vat_ok was trivially True) and receipt_paper_width_mm is NOT NULL with
        # a server default of 80mm (so paper_ok was trivially True even though
        # nobody had ever opened receipt settings). Paper width is no longer
        # used as evidence of configuration for exactly that reason. Now
        # requires actual evidence the owner touched receipt settings (a
        # trading name, or any receipt_config_json at all), and — if
        # VAT-registered — a real VAT number AND a non-zero rate (a 0% VAT tax
        # invoice is invalid; see routes_business.py's matching VAT_RATE_REQUIRED
        # validation on PUT /settings).
        receipt_config = business.receipt_config_json or {}
        receipt_configured = bool(receipt_config.get("trading_name")) or bool(receipt_config)
        if business.vat_registered:
            vat_ok = bool(business.vat_number) and business.vat_rate_basis_points > 0
        else:
            vat_ok = True

        if vat_ok and receipt_configured:
            detail = "VAT and receipt settings are configured"
            status = STATUS_PASS
        elif not vat_ok:
            if not business.vat_number:
                detail = "Business is marked VAT-registered but has no VAT number set"
            else:
                detail = "Business is marked VAT-registered but its VAT rate is 0% — a tax invoice needs a real rate"
            status = STATUS_FAIL
        else:
            detail = "Receipt settings have not been configured yet (e.g. trading name)"
            status = STATUS_FAIL
        checks.append(GoLiveCheck(key="vat_receipt", label="VAT & receipt settings", status=status, detail=detail))

        # ── At least one POS payment method enabled ─────────────────────────
        pos_methods = [
            business.pos_cash_enabled, business.pos_card_enabled, business.pos_payfast_enabled,
            business.pos_yoco_enabled, business.pos_ikhoka_enabled, business.pos_direct_eft_enabled,
            business.pos_other_payment_enabled,
        ]
        checks.append(GoLiveCheck(
            key="pos_payment_method", label="POS payment method",
            status=STATUS_PASS if any(pos_methods) else STATUS_FAIL,
            detail="At least one till payment method is enabled" if any(pos_methods)
                   else "No till payment method is enabled — the till cannot take a sale",
        ))

        # ── A till session has been opened at least once ───────────────────
        till_opened = int((await db.execute(
            select(func.count()).select_from(TillSession).where(TillSession.business_id == business_id)
        )).scalar() or 0)
        checks.append(GoLiveCheck(
            key="till_session", label="Till opened",
            status=STATUS_PASS if till_opened else STATUS_FAIL,
            detail=f"{till_opened} till session(s) opened" if till_opened else "The till has never been opened",
        ))

        # ── At least one POS test order + a receipt document can be built ──
        pos_order_result = await db.execute(
            select(Order).where(
                Order.business_id == business_id, Order.source == OrderSource.POS.value,
            ).order_by(Order.created_at.desc()).limit(1)
        )
        pos_order = pos_order_result.scalar_one_or_none()
        if not pos_order:
            checks.append(GoLiveCheck(
                key="pos_test_order", label="POS test order",
                status=STATUS_FAIL, detail="No POS order has been rung up yet",
            ))
        else:
            try:
                from backend.app.receipts.document import build_receipt_document
                await db.refresh(pos_order, attribute_names=["items"])
                build_receipt_document(pos_order, business, order_items=pos_order.items)
                checks.append(GoLiveCheck(
                    key="pos_test_order", label="POS test order",
                    status=STATUS_PASS, detail="A POS order exists and its receipt renders correctly",
                ))
            except Exception as exc:  # noqa: BLE001 — intentionally broad: any receipt-build failure fails this check
                checks.append(GoLiveCheck(
                    key="pos_test_order", label="POS test order",
                    status=STATUS_FAIL, detail=f"A POS order exists but its receipt failed to build: {exc}",
                ))
    else:
        checks.append(GoLiveCheck(key="vat_receipt", label="VAT & receipt settings", status=STATUS_NA, detail="POS is off for this business"))
        checks.append(GoLiveCheck(key="pos_payment_method", label="POS payment method", status=STATUS_NA, detail="POS is off for this business"))
        checks.append(GoLiveCheck(key="till_session", label="Till opened", status=STATUS_NA, detail="POS is off for this business"))
        checks.append(GoLiveCheck(key="pos_test_order", label="POS test order", status=STATUS_NA, detail="POS is off for this business"))

    # ── Inventory (only if the module is switched on) ──────────────────────
    if business.inventory_enabled:
        ingredient_count = int((await db.execute(
            select(func.count()).select_from(Ingredient).where(
                Ingredient.business_id == business_id, Ingredient.is_deleted.is_(False), Ingredient.is_active.is_(True),
            )
        )).scalar() or 0)
        checks.append(GoLiveCheck(
            key="ingredients", label="Ingredients",
            status=STATUS_PASS if ingredient_count else STATUS_FAIL,
            detail=f"{ingredient_count} ingredient(s)" if ingredient_count else "No ingredients recorded yet",
        ))

        items_with_recipe = {
            r.menu_item_id for r in (
                await db.execute(
                    select(Recipe).where(
                        Recipe.business_id == business_id, Recipe.is_active.is_(True), Recipe.menu_item_id.isnot(None),
                    )
                )
            ).scalars().all()
        }
        covered = int((await db.execute(
            select(func.count()).select_from(MenuItem).where(
                MenuItem.business_id == business_id, MenuItem.is_deleted.is_(False), MenuItem.is_active.is_(True),
                MenuItem.id.in_(items_with_recipe or [uuid.uuid4()]),
            )
        )).scalar() or 0) if active_items else 0
        recipe_ok = active_items > 0 and covered == active_items
        checks.append(GoLiveCheck(
            key="recipe_coverage", label="Recipe coverage",
            status=STATUS_PASS if recipe_ok else (STATUS_WARN if covered else STATUS_FAIL),
            detail=f"{covered} of {active_items} active menu item(s) have a recipe",
        ))

        opening_stocktake = int((await db.execute(
            select(func.count()).select_from(StocktakeSession).where(
                StocktakeSession.business_id == business_id, StocktakeSession.status == "APPROVED",
            )
        )).scalar() or 0)
        checks.append(GoLiveCheck(
            key="opening_stocktake", label="Opening stocktake",
            status=STATUS_PASS if opening_stocktake else STATUS_FAIL,
            detail=f"{opening_stocktake} approved count(s)" if opening_stocktake else "No approved opening stocktake yet",
        ))
    else:
        checks.append(GoLiveCheck(key="ingredients", label="Ingredients", status=STATUS_NA, detail="Inventory module is off"))
        checks.append(GoLiveCheck(key="recipe_coverage", label="Recipe coverage", status=STATUS_NA, detail="Inventory module is off"))
        checks.append(GoLiveCheck(key="opening_stocktake", label="Opening stocktake", status=STATUS_NA, detail="Inventory module is off"))

    # ── WhatsApp (only if the module is switched on) ───────────────────────
    if business.is_whatsapp_enabled:
        checks.append(GoLiveCheck(
            key="whatsapp_phone_number", label="WhatsApp phone number",
            status=STATUS_PASS if business.whatsapp_phone_number_id else STATUS_FAIL,
            detail="Configured" if business.whatsapp_phone_number_id else "WhatsApp is on but no phone_number_id is set",
        ))
    else:
        checks.append(GoLiveCheck(key="whatsapp_phone_number", label="WhatsApp phone number", status=STATUS_NA, detail="WhatsApp is off for this business"))

    # ── Billing status ──────────────────────────────────────────────────────
    billing_ok = business.billing_status in ("TRIAL", "ACTIVE")
    checks.append(GoLiveCheck(
        key="billing_status", label="Billing status",
        status=STATUS_PASS if billing_ok else STATUS_FAIL,
        detail=f"Billing status is {business.billing_status}",
    ))

    # ── Restaurant details (Wave 0A commercial push) ────────────────────────
    details_ok = bool(business.name) and bool(business.address or business.phone)
    checks.append(GoLiveCheck(
        key="restaurant_details", label="Restaurant details",
        status=STATUS_PASS if details_ok else STATUS_FAIL,
        detail="Name and at least one of address/phone are recorded" if details_ok
               else "Missing a restaurant name and/or both address and phone",
    ))

    # ── Manual confirmations the system cannot observe automatically ───────
    confirmations = business.onboarding_confirmations or {}

    # Real-browser fix: the detail text used to show the confirming admin's
    # raw UUID (confirmed_by is stored as a bare id — see
    # routes_businesses.py::confirm_onboarding_item). Resolve every id
    # actually referenced to its current display_name in one query.
    # Null-safe: a missing/malformed id, or an admin row that no longer
    # exists (e.g. deleted since), falls back to a plain label rather than
    # raising or ever showing a raw UUID.
    confirmed_by_name: dict[str, str] = {}
    _raw_admin_ids = {
        c.get("confirmed_by") for c in confirmations.values()
        if isinstance(c, dict) and c.get("confirmed_by")
    }
    _admin_ids: list[uuid.UUID] = []
    for _raw in _raw_admin_ids:
        try:
            _admin_ids.append(uuid.UUID(str(_raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    if _admin_ids:
        _admin_rows = (await db.execute(
            select(AdminUser.id, AdminUser.display_name).where(AdminUser.id.in_(_admin_ids))
        )).all()
        confirmed_by_name = {str(admin_id): name for admin_id, name in _admin_rows}

    def _confirmed_by_label(item_key: str) -> str:
        raw = confirmations.get(item_key, {}).get("confirmed_by")
        if not raw:
            return "an admin"
        try:
            # Normalise through uuid.UUID so both the hex-no-dashes form
            # (routes_businesses.py::confirm_onboarding_item stores
            # user.user_id.hex) and a standard dashed string match the
            # same dict key.
            normalized = str(uuid.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            return "an admin"
        return confirmed_by_name.get(normalized, "an admin (account no longer active)")

    modules_confirmed = "modules" in confirmations
    checks.append(GoLiveCheck(
        key="modules_confirmed", label="Purchased modules confirmed",
        status=STATUS_PASS if modules_confirmed else STATUS_FAIL,
        detail=(
            f"Confirmed by {_confirmed_by_label('modules')} "
            f"on {confirmations['modules'].get('confirmed_at', 'unknown date')}"
        ) if modules_confirmed else
        "No admin has confirmed which modules this client purchased yet — "
        "the system cannot infer this, see POST .../onboarding/confirm",
    ))

    # ── Receipt test / Cash-up test (POS-dependent, see pos_enabled gate ────
    # above) — a WhatsApp-only tenant has no till at all, so both are N/A.
    if business.pos_enabled:
        # Receipt test: a real receipt renders AND a human confirmed the
        # physical print (the print itself happens in a browser — invisible
        # to the server — so this check is deliberately BOTH a real-data
        # check (pos_test_order, above) AND a manual confirmation, not
        # either alone.
        receipt_print_confirmed = "receipt_print" in confirmations
        receipt_renders = any(c.key == "pos_test_order" and c.status == STATUS_PASS for c in checks)
        if receipt_renders and receipt_print_confirmed:
            checks.append(GoLiveCheck(
                key="receipt_test", label="Receipt print test",
                status=STATUS_PASS,
                detail="A receipt renders correctly and a human confirmed the physical print",
            ))
        elif receipt_renders:
            checks.append(GoLiveCheck(
                key="receipt_test", label="Receipt print test",
                status=STATUS_WARN,
                detail="A receipt renders correctly, but no one has confirmed the physical print test",
            ))
        else:
            checks.append(GoLiveCheck(
                key="receipt_test", label="Receipt print test",
                status=STATUS_FAIL, detail="No receipt has been built from a real POS order yet",
            ))

        # Cashup test: at least one CLOSED till session (distinct from the
        # existing till_session check above, which only requires one ever opened)
        closed_till_count = int((await db.execute(
            select(func.count()).select_from(TillSession).where(
                TillSession.business_id == business_id, TillSession.status == "CLOSED",
            )
        )).scalar() or 0)
        checks.append(GoLiveCheck(
            key="cashup_test", label="Cash-up test",
            status=STATUS_PASS if closed_till_count else STATUS_FAIL,
            detail=f"{closed_till_count} closed till session(s)" if closed_till_count
                   else "No till session has been closed (cashed up) yet",
        ))
    else:
        checks.append(GoLiveCheck(key="receipt_test", label="Receipt print test", status=STATUS_NA, detail="POS is off for this business"))
        checks.append(GoLiveCheck(key="cashup_test", label="Cash-up test", status=STATUS_NA, detail="POS is off for this business"))

    # ── WhatsApp test: a real outbound message reached SENT, or a human ────
    # confirmed a manual test send (the test-send endpoint has no DB
    # persistence, see routes_businesses.py::send_whatsapp_test, so a
    # confirmation is the only server-observable record of it happening).
    if business.is_whatsapp_enabled:
        whatsapp_confirmed = "whatsapp_test" in confirmations
        sent_message_count = int((await db.execute(
            select(func.count()).select_from(MessageOutbox).where(
                MessageOutbox.business_id == business_id, MessageOutbox.status == "SENT",
            )
        )).scalar() or 0)
        whatsapp_ok = whatsapp_confirmed or sent_message_count > 0
        checks.append(GoLiveCheck(
            key="whatsapp_test", label="WhatsApp test message",
            status=STATUS_PASS if whatsapp_ok else STATUS_FAIL,
            detail=(
                f"{sent_message_count} outbound message(s) reached SENT" if sent_message_count
                else f"Confirmed by {_confirmed_by_label('whatsapp_test')}" if whatsapp_confirmed
                else "No outbound WhatsApp message has been sent yet, and no test-send has been confirmed"
            ),
        ))
    else:
        checks.append(GoLiveCheck(
            key="whatsapp_test", label="WhatsApp test message",
            status=STATUS_NA, detail="WhatsApp is off for this business",
        ))

    # ── Payment methods — already covered by pos_payment_method above; not ──
    # duplicated here (see this function's module docstring / task DOC NOTE).

    passed = sum(1 for c in checks if c.status == STATUS_PASS)
    failed = sum(1 for c in checks if c.status == STATUS_FAIL)
    warned = sum(1 for c in checks if c.status == STATUS_WARN)
    ready = failed == 0 and all(c.status in (STATUS_PASS, STATUS_WARN, STATUS_NA) for c in checks)

    applicable = [c for c in checks if c.status != STATUS_NA]
    readiness_total = len(applicable)
    readiness_passed = sum(1 for c in applicable if c.status == STATUS_PASS)
    readiness_percent = round(100 * readiness_passed / readiness_total) if readiness_total else 100
    readiness = ReadinessSummary(
        passed=readiness_passed, total=readiness_total, percent=readiness_percent,
        label=f"Restaurant Ready — {readiness_passed}/{readiness_total} checks complete",
    )

    return GoLiveResult(
        ready=ready, passed=passed, failed=failed, warned=warned, checks=checks, readiness=readiness,
    )
