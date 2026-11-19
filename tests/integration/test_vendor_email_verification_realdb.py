"""
Real-Postgres tests for souk vendor email verification (migration 0070,
backend/app/services/email_verification.py, routes_email_verification.py).

Route functions are called through ``__wrapped__`` with a real session (the
pattern of test_event_phase2_realdb.py), plus HTTP-level tests through the
real app (TestClient without ``with``, raise_server_exceptions=False) so the
public routes' JSON body handling and annotations cannot regress.

Email goes to an InMemoryEmailSender installed per test; nothing is delivered.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from admin_api.app.api.v1.routes_events import MarkPaid, mark_subscription_paid
from backend.app.api.v1 import routes_email_verification as ver
from backend.app.api.v1 import routes_event_public as pub
from backend.app.api.v1 import routes_event_vendor as ven
from backend.app.api.v1.routes_business import get_event_access
from backend.app.core.errors import AppError
from backend.app.email.sender import InMemoryEmailSender, set_email_sender
from backend.app.events.service import business_access_decision
from backend.app.events.trading import assert_trading_allowed
from backend.app.services import email_verification as ev
from backend.app.services.auth_service import AuthService
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.event import EventVendor, EventVendorSubscription
from shared.models.user import BusinessUser, EmailVerificationToken
from tests.integration._db import get_session_factory
from tests.integration._env import get_test_database_url
from tests.integration.test_event_phase2_realdb import (  # noqa: F401 - bag is a fixture
    ADMIN,
    DAYS,
    PASSWORD,
    _body,
    _open_event,
    _owner,
    _register,
    bag,
)
from tests.integration.test_souk_security_review_realdb import _row, _vendors

pytestmark = pytest.mark.asyncio

_REGISTER = pub.register_vendor.__wrapped__
_VERIFY = ver.verify_email.__wrapped__
_RESEND = ver.resend_verification.__wrapped__


@pytest.fixture(autouse=True)
def outbox():
    sender = InMemoryEmailSender()
    set_email_sender(sender)
    yield sender
    set_email_sender(None)


@pytest.fixture
async def extra(db):
    """Businesses whose owner email was tombstoned (cannot be found by email for cleanup)."""
    ids: list[uuid.UUID] = []
    yield ids
    await db.rollback()
    if ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id.in_(ids)))
        await db.execute(delete(Business).where(Business.id.in_(ids)))
        await db.commit()


def _token_from(message) -> str:
    assert "#token=" in message.text
    return message.text.split("#token=", 1)[1].split()[0]


def _mails(outbox, kind, to=None):
    return [m for m in outbox.outbox if m.kind == kind and (to is None or m.to == to.lower())]


async def _refused(coro) -> AppError:
    with pytest.raises(AppError) as exc:
        await coro
    return exc.value


async def _owner_row(db, user_id) -> BusinessUser:
    stmt = select(BusinessUser).where(BusinessUser.id == user_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def _biz(db, business_id) -> Business:
    stmt = select(Business).where(Business.id == business_id).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one()


async def _pay_today(db, ev_, vendor):
    """Quote today..+1 as the vendor and have the admin mark it paid (the only path to TRADE)."""
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=ev_["id"], starts_on=DAYS[0], ends_on=DAYS[1]), vendor["user"], db)
    await mark_subscription_paid(ev_["id"], sub["id"], MarkPaid(payment_reference=f"EFT-{uuid.uuid4().hex[:10]}"),
                                 ADMIN, db)
    return sub


async def _backdate(db, user_id, days):
    await db.execute(update(BusinessUser).where(BusinessUser.id == user_id)
                     .values(created_at=datetime.now(timezone.utc) - timedelta(days=days)))
    await db.commit()


# ── Registration sends a verification email ───────────────────────────────────

async def test_registration_requires_verification_and_stores_only_a_hash_of_the_token(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    owner = await _owner_row(db, v["owner_id"])
    assert owner.email_verification_required is True and owner.email_verified_at is None
    [mail] = _mails(outbox, "email_verification", v["email"])
    raw = _token_from(mail)
    assert len(raw) >= 43  # 32 bytes, urlsafe base64
    assert v["business_code"] in mail.text  # the code moved from the response to the email
    rows = (await db.execute(select(EmailVerificationToken).where(
        EmailVerificationToken.user_id == owner.id))).scalars().all()
    assert len(rows) == 1 and rows[0].token_hash == ev.hash_token(raw) and raw not in rows[0].token_hash
    ttl = rows[0].expires_at - rows[0].created_at
    assert ttl == timedelta(hours=24)
    audits = (await db.execute(select(AuditEvent).where(AuditEvent.business_id == v["business_id"]))).scalars().all()
    assert raw not in repr([(a.action, a.diff_json) for a in audits])
    assert "auth.email_verification_sent" in {a.action for a in audits}


# ── verify-email: invalid, expired, reused, superseded all look the same ─────

async def test_valid_token_verifies_once_and_reuse_gets_the_generic_error(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    raw = _token_from(_mails(outbox, "email_verification", v["email"])[0])
    ok = await _VERIFY(ver.VerifyEmailRequest(token=raw), None, db)
    assert ok == {"verified": True, "next": "login", "business_code": v["business_code"]}
    assert (await _owner_row(db, v["owner_id"])).email_verified_at is not None
    reused = await _refused(_VERIFY(ver.VerifyEmailRequest(token=raw), None, db))
    assert (reused.code, reused.status_code) == ("VERIFICATION_TOKEN_INVALID", 400)
    actions = (await db.execute(select(AuditEvent.action).where(AuditEvent.business_id == v["business_id"]))).scalars().all()
    assert actions.count("auth.email_verified") == 1


async def test_invalid_expired_reused_and_superseded_tokens_are_indistinguishable(db, bag, outbox):
    event = await _open_event(db, bag)
    a = await _register(db, bag, event, verify=False)
    b = await _register(db, bag, event, verify=False)
    c = await _register(db, bag, event, verify=False)
    now = datetime.now(timezone.utc)
    # Expired: issued 25 hours ago.
    expired = await ev.issue_token(db, a["owner_id"], now - timedelta(hours=25))
    await db.commit()
    # Reused: spend B's token once.
    used = _token_from(_mails(outbox, "email_verification", b["email"])[0])
    await _VERIFY(ver.VerifyEmailRequest(token=used), None, db)
    # Superseded: C's emailed token, replaced by a newer one.
    superseded = _token_from(_mails(outbox, "email_verification", c["email"])[0])
    await ev.issue_token(db, c["owner_id"])
    await db.commit()
    errors = [await _refused(_VERIFY(ver.VerifyEmailRequest(token=t), None, db))
              for t in ("not-a-real-token", expired, used, superseded, "x" * 256)]
    assert {(e.code, e.message, e.status_code) for e in errors} == {(
        "VERIFICATION_TOKEN_INVALID", "This verification link is invalid or has expired. Request a new one.", 400)}
    # Nobody but B got verified by any of that.
    assert (await _owner_row(db, a["owner_id"])).email_verified_at is None
    assert (await _owner_row(db, c["owner_id"])).email_verified_at is None


async def test_a_token_only_ever_verifies_its_own_user(db, bag, outbox):
    event = await _open_event(db, bag)
    a = await _register(db, bag, event, verify=False)
    b = await _register(db, bag, event, verify=False)
    raw_a = _token_from(_mails(outbox, "email_verification", a["email"])[0])
    raw_b = _token_from(_mails(outbox, "email_verification", b["email"])[0])
    await _VERIFY(ver.VerifyEmailRequest(token=raw_a), None, db)
    assert (await _owner_row(db, a["owner_id"])).email_verified_at is not None
    assert (await _owner_row(db, b["owner_id"])).email_verified_at is None
    # B's own token is untouched by A's verification and still works for B only.
    b_tok = (await db.execute(select(EmailVerificationToken).where(
        EmailVerificationToken.token_hash == ev.hash_token(raw_b)))).scalar_one()
    assert b_tok.used_at is None and b_tok.invalidated_at is None
    assert (await _VERIFY(ver.VerifyEmailRequest(token=raw_b), None, db))["business_code"] == b["business_code"]


async def test_two_concurrent_verifications_with_one_token_succeed_exactly_once(db, bag, outbox):
    event = await _open_event(db, bag)
    factory = get_session_factory()
    for _ in range(5):
        v = await _register(db, bag, event, verify=False)
        raw = _token_from(_mails(outbox, "email_verification", v["email"])[-1])

        async def attempt(token=raw):
            async with factory() as s:
                try:
                    await _VERIFY(ver.VerifyEmailRequest(token=token), None, s)
                    return "ok"
                except AppError as exc:
                    return exc.code

        results = await asyncio.gather(attempt(), attempt(), attempt())
        assert sorted(results) == ["VERIFICATION_TOKEN_INVALID", "VERIFICATION_TOKEN_INVALID", "ok"]
        n = (await db.execute(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.business_id == v["business_id"], AuditEvent.action == "auth.email_verified"))).scalar()
        assert n == 1


# ── resend-verification ───────────────────────────────────────────────────────

async def test_resend_answers_identically_for_unknown_verified_and_unverified_emails(db, bag, outbox):
    event = await _open_event(db, bag)
    pending = await _register(db, bag, event, verify=False)
    done = await _register(db, bag, event)  # verified
    await db.execute(update(EmailVerificationToken).where(EmailVerificationToken.user_id == pending["owner_id"])
                     .values(created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db.commit()
    outbox.clear()
    answers = [await _RESEND(ver.ResendVerificationRequest(email=e), None, db)
               for e in (f"nobody-{uuid.uuid4().hex[:6]}@example.com", done["email"], pending["email"])]
    assert answers[0] == answers[1] == answers[2] == ver.RESEND_ACCEPTED
    assert [m.to for m in outbox.outbox] == [pending["email"].lower()]
    assert outbox.outbox[0].kind == "email_verification"


async def test_resend_invalidates_older_tokens(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    first = _token_from(_mails(outbox, "email_verification", v["email"])[0])
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    assert await ev.request_resend(db, v["email"], later) is True
    second = _token_from(_mails(outbox, "email_verification", v["email"])[-1])
    assert first != second
    assert (await _refused(_VERIFY(ver.VerifyEmailRequest(token=first), None, db))).code == "VERIFICATION_TOKEN_INVALID"
    await _VERIFY(ver.VerifyEmailRequest(token=second), None, db)


async def test_resend_cooldown_and_hourly_cap_are_silent(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    t0 = (await db.execute(select(func.max(EmailVerificationToken.created_at)).where(
        EmailVerificationToken.user_id == v["owner_id"]))).scalar_one()
    sent = lambda: len(_mails(outbox, "email_verification", v["email"]))  # noqa: E731
    assert sent() == 1
    # Inside the 60 s cooldown: nothing sent, and the route still says 202 the same way.
    assert await ev.request_resend(db, v["email"], t0 + timedelta(seconds=30)) is False
    assert sent() == 1
    # Four more, each after the cooldown: 5 tokens inside one hour.
    for i in range(1, 5):
        assert await ev.request_resend(db, v["email"], t0 + timedelta(seconds=61 * i)) is True
    assert sent() == 5
    # Sixth inside the hour, after the cooldown: capped silently.
    assert await ev.request_resend(db, v["email"], t0 + timedelta(seconds=61 * 5 + 10)) is False
    assert sent() == 5
    # Once the earliest ones leave the one-hour window, sending resumes.
    assert await ev.request_resend(db, v["email"], t0 + timedelta(hours=1, seconds=200)) is True
    assert sent() == 6
    # The route answer when throttled is the same accepted body.
    assert await _RESEND(ver.ResendVerificationRequest(email=v["email"]), None, db) == ver.RESEND_ACCEPTED


async def test_resend_is_rate_limited_per_ip_and_has_a_per_email_budget():
    for fn, expected in ((_VERIFY, "10"), (_RESEND, "3")):
        name = f"{fn.__module__}.{fn.__name__}"
        configured = [str(i.limit) for i in pub.limiter._route_limits.get(name, [])]
        assert configured and any(expected in c for c in configured), name
    assert any("hour" in str(i.limit) for i in pub.limiter._route_limits[f"{_RESEND.__module__}.{_RESEND.__name__}"])
    assert ver.email_budget_key(" A@Example.com ") == ver.email_budget_key("a@example.com")
    assert "@" not in ver.email_budget_key("a@example.com")


# ── Register: duplicate email ─────────────────────────────────────────────────

async def test_duplicate_email_gets_the_same_answer_creates_nothing_and_notifies_the_owner(db, bag, outbox):
    event = await _open_event(db, bag)
    first = await _register(db, bag, event)
    outbox.clear()
    before = (await db.execute(select(func.count()).select_from(Business))).scalar()
    users_before = (await db.execute(select(func.count()).select_from(BusinessUser))).scalar()
    answers = []
    for _ in range(4):
        body = _body(bag, event["code"], owner_email=first["email"].upper(), password="Other-password-9")
        answers.append(await _REGISTER(event["slug"], body, None, db))
    assert all(a == first["response"] for a in answers)
    assert (await db.execute(select(func.count()).select_from(Business))).scalar() == before
    assert (await db.execute(select(func.count()).select_from(BusinessUser))).scalar() == users_before
    # The owner is told, at most DUPLICATE_NOTICE_HOURLY_CAP times an hour; no verification link is sent.
    assert len(_mails(outbox, "duplicate_registration", first["email"])) == ev.DUPLICATE_NOTICE_HOURLY_CAP
    assert _mails(outbox, "email_verification") == []
    # The existing account's password did not change.
    await AuthService(db).login_email(first["email"], PASSWORD)


# ── Trading and payment claims wait for verification ─────────────────────────

async def test_trading_is_refused_with_email_not_verified_until_verified(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    # Login is allowed before verification (so the vendor can configure).
    await AuthService(db).login_email(v["email"], PASSWORD)
    await _pay_today(db, event, v)
    err = await _refused(assert_trading_allowed(db, v["business_id"]))
    assert err.code == "EVENT_TRADING_NOT_ALLOWED" and err.details["reason"] == "EMAIL_NOT_VERIFIED"
    assert err.details["access_level"] == "CONFIGURE"
    access = await get_event_access(v["user"], db)
    assert (access["access_level"], access["reason"]) == ("CONFIGURE", "EMAIL_NOT_VERIFIED")
    assert access["email_verification"] == {"required": True, "verified": False}
    # Offline sync is gated the same way (through assert_trading_allowed).
    err = await _refused(assert_trading_allowed(db, v["business_id"], captured_at=datetime.now(timezone.utc)))
    assert err.details["reason"] == "EMAIL_NOT_VERIFIED"

    raw = _token_from(_mails(outbox, "email_verification", v["email"])[0])
    await _VERIFY(ver.VerifyEmailRequest(token=raw), None, db)
    await assert_trading_allowed(db, v["business_id"])
    access = await get_event_access(v["user"], db)
    assert (access["access_level"], access["reason"]) == ("TRADE", "PAID_DAY")
    assert access["email_verification"] == {"required": True, "verified": True}


async def test_unverified_vendor_on_an_unpaid_day_keeps_the_more_fundamental_reason(db, bag):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    decision = await business_access_decision(db, v["business_id"])
    assert (decision.level, decision.reason) == ("CONFIGURE", "NO_PAID_COVER_TODAY")


async def test_payment_claim_is_refused_until_verified(db, bag, outbox):
    event = await _open_event(db, bag)
    v = await _register(db, bag, event, verify=False)
    sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=event["id"], starts_on=DAYS[0], ends_on=DAYS[0]), v["user"], db)
    err = await _refused(ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-123"), v["user"], db))
    assert (err.code, err.status_code) == ("EMAIL_NOT_VERIFIED", 403)
    await db.rollback()
    row = (await db.execute(select(EventVendorSubscription).where(EventVendorSubscription.id == sub["id"]))).scalar_one()
    assert row.payment_claim_reference is None
    raw = _token_from(_mails(outbox, "email_verification", v["email"])[0])
    await _VERIFY(ver.VerifyEmailRequest(token=raw), None, db)
    out = await ven.claim_payment(sub["id"], ven.PaymentClaim(reference="EFT-123"), v["user"], db)
    assert out["payment_claim_reference"] == "EFT-123"


async def test_admin_provisioned_vendors_and_restaurants_are_unaffected(db, bag, test_business):
    from tests.integration.test_souk_security_review_realdb import _event, _pay
    event = await _event(db, bag)
    [vendor] = await _vendors(db, event, bag, [_row(bag, starts=DAYS[0], ends=DAYS[1])])
    owner = (await db.execute(select(BusinessUser).where(BusinessUser.business_id == vendor["business_id"]))).scalar_one()
    assert owner.email_verification_required is False
    await _pay(db, event, vendor)
    await assert_trading_allowed(db, vendor["business_id"])
    access = await get_event_access(_owner(vendor["business_id"]), db)
    assert access["access_level"] == "TRADE" and access["email_verification"] == {"required": False, "verified": True}
    # A restaurant: never event-gated, no verification required.
    await assert_trading_allowed(db, test_business.id)
    plain = await get_event_access(_owner(test_business.id), db)
    assert plain["is_event_vendor"] is False and plain["email_verification"]["required"] is False


# ── Abandoned registrations ───────────────────────────────────────────────────

async def test_abandoned_cleanup_closes_only_old_unverified_unpaid_self_registrations(db, bag, outbox):
    from tests.integration.test_souk_security_review_realdb import _event
    event = await _open_event(db, bag)
    stale = await _register(db, bag, event, verify=False)
    stale_sub = await ven.create_event_subscription(
        ven.SubscriptionRequest(event_id=event["id"], starts_on=DAYS[3], ends_on=DAYS[3]), stale["user"], db)
    paid = await _register(db, bag, event, verify=False)
    await _pay_today(db, event, paid)
    verified = await _register(db, bag, event)
    young = await _register(db, bag, event, verify=False)
    admin_ev = await _event(db, bag)
    [admin_vendor] = await _vendors(db, admin_ev, bag, [_row(bag)])
    admin_owner = (await db.execute(select(BusinessUser.id).where(
        BusinessUser.business_id == admin_vendor["business_id"]))).scalar_one()
    for uid in (stale["owner_id"], paid["owner_id"], verified["owner_id"], admin_owner):
        await _backdate(db, uid, 8)
    await _backdate(db, young["owner_id"], 6)
    stale_token = _token_from(_mails(outbox, "email_verification", stale["email"])[0])

    result = await ev.close_abandoned_registrations(db)
    await db.commit()
    assert stale["owner_id"] in result.closed_user_ids
    assert not {paid["owner_id"], verified["owner_id"], young["owner_id"], admin_owner} & set(result.closed_user_ids)

    owner = await _owner_row(db, stale["owner_id"])
    assert owner.registration_abandoned_at is not None and owner.is_active is False
    assert owner.email == stale["email"].lower()  # not released until somebody re-registers
    biz = await _biz(db, stale["business_id"])
    assert (biz.is_active, biz.suspended_reason) == (False, ev.ABANDONED_SUSPENSION_REASON)
    sub = (await db.execute(select(EventVendorSubscription).where(
        EventVendorSubscription.id == stale_sub["id"]).execution_options(populate_existing=True))).scalar_one()
    assert sub.status == "VOID"
    vendor = (await db.execute(select(EventVendor).where(EventVendor.business_id == stale["business_id"])
                               .execution_options(populate_existing=True))).scalar_one()
    assert vendor.status == "CANCELLED"
    audit = (await db.execute(select(AuditEvent).where(
        AuditEvent.business_id == stale["business_id"], AuditEvent.action == "auth.registration_abandoned_closed"))).scalar_one()
    assert audit.diff_json["voided_subscriptions"] == 1 and stale["email"] not in repr(audit.diff_json)
    # The old link no longer verifies, and the account cannot log in.
    assert (await _refused(_VERIFY(ver.VerifyEmailRequest(token=stale_token), None, db))).code == "VERIFICATION_TOKEN_INVALID"
    assert (await _refused(AuthService(db).login_email(stale["email"], PASSWORD))).code == "ACCOUNT_DISABLED"
    # Untouched accounts.
    for v in (paid, verified, young):
        assert (await _owner_row(db, v["owner_id"])).is_active is True
        assert (await _owner_row(db, v["owner_id"])).registration_abandoned_at is None
    assert (await _biz(db, paid["business_id"])).is_active is True
    assert (await _owner_row(db, admin_owner)).registration_abandoned_at is None
    # Idempotent.
    assert (await ev.close_abandoned_registrations(db)).closed_user_ids == []
    # The hourly reconcile never re-activates an abandoned business.
    from backend.app.events.service import reconcile_single_business_access
    await reconcile_single_business_access(db, stale["business_id"])
    await db.commit()
    assert (await _biz(db, stale["business_id"])).is_active is False


async def test_re_registration_after_abandonment_takes_the_email_over_safely(db, bag, outbox, extra):
    event = await _open_event(db, bag)
    old = await _register(db, bag, event, verify=False)
    extra.append(old["business_id"])
    await _backdate(db, old["owner_id"], 8)
    await ev.close_abandoned_registrations(db)
    await db.commit()
    outbox.clear()

    body = _body(bag, event["code"], owner_email=old["email"], password="Brand-new-pw-22")
    res = await _REGISTER(event["slug"], body, None, db)
    assert res["next"] == "check_email"
    new_owner = (await db.execute(select(BusinessUser).where(
        BusinessUser.email == old["email"].lower()).execution_options(populate_existing=True))).scalar_one()
    assert new_owner.id != old["owner_id"] and new_owner.business_id != old["business_id"]
    assert new_owner.email_verification_required is True and new_owner.email_verified_at is None
    released = await _owner_row(db, old["owner_id"])
    assert released.email is None and released.is_active is False
    assert (await _biz(db, old["business_id"])).is_active is False
    assert "auth.abandoned_registration_email_released" in (await db.execute(select(AuditEvent.action).where(
        AuditEvent.business_id == old["business_id"]))).scalars().all()
    raw = _token_from(_mails(outbox, "email_verification", old["email"])[0])
    await _VERIFY(ver.VerifyEmailRequest(token=raw), None, db)
    user, business, _, _ = await AuthService(db).login_email(old["email"], "Brand-new-pw-22")
    assert business.id == new_owner.business_id


async def test_re_registration_can_never_take_over_a_verified_pending_or_admin_account(db, bag, outbox):
    from tests.integration.test_souk_security_review_realdb import _event
    event = await _open_event(db, bag)
    verified = await _register(db, bag, event)
    pending = await _register(db, bag, event, verify=False)  # unverified but NOT abandoned
    admin_ev = await _event(db, bag)
    row = _row(bag)
    [admin_vendor] = await _vendors(db, admin_ev, bag, [row])
    # Even an old admin-provisioned owner (never verified, required=False) is not releasable.
    admin_owner = (await db.execute(select(BusinessUser).where(
        BusinessUser.business_id == admin_vendor["business_id"]))).scalar_one()
    await _backdate(db, admin_owner.id, 30)
    await ev.close_abandoned_registrations(db)
    await db.commit()
    for email, owner_id in ((verified["email"], verified["owner_id"]), (pending["email"], pending["owner_id"]),
                            (str(row.owner_email), admin_owner.id)):
        before = (await db.execute(select(func.count()).select_from(Business))).scalar()
        res = await _REGISTER(event["slug"], _body(bag, event["code"], owner_email=email, password="Hijack-pw-123"), None, db)
        assert res["next"] == "check_email"
        assert (await db.execute(select(func.count()).select_from(Business))).scalar() == before
        holder = (await db.execute(select(BusinessUser).where(BusinessUser.email == email.lower())
                                   .execution_options(populate_existing=True))).scalar_one()
        assert holder.id == owner_id
        with pytest.raises(AppError):
            await AuthService(db).login_email(email, "Hijack-pw-123")
    # The defence-in-depth check refuses outright too.
    with pytest.raises(ValueError):
        await ev.release_abandoned_email(db, await _owner_row(db, verified["owner_id"]))
    await db.rollback()


# ── HTTP through the real app ─────────────────────────────────────────────────

@pytest.fixture
def client():
    """The real app over HTTP with get_db bound to the test database (NullPool: TestClient runs its own loop)."""
    from fastapi.testclient import TestClient

    from backend.app.db.session import get_db
    from backend.app.main import app

    engine = create_async_engine(get_test_database_url(), poolclass=NullPool,
                                 connect_args={"statement_cache_size": 0})
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_http_register_verify_and_resend_round_trip(db, bag, outbox, client):
    event = await _open_event(db, bag)
    body = _body(bag, event["code"]).model_dump(mode="json")
    r = await asyncio.to_thread(client.post, f"/v1/public/events/{event['slug']}/register", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["next"] == "check_email" and "business_code" not in r.json()
    dup = await asyncio.to_thread(client.post, f"/v1/public/events/{event['slug']}/register",
                                  json={**body, "password": "Another-pw-77"})
    assert (dup.status_code, dup.json()) == (202, r.json())

    raw = _token_from(_mails(outbox, "email_verification", body["owner_email"])[0])
    bad = await asyncio.to_thread(client.post, "/v1/public/auth/verify-email", json={"token": "nope"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "VERIFICATION_TOKEN_INVALID"
    ok = await asyncio.to_thread(client.post, "/v1/public/auth/verify-email", json={"token": raw})
    assert ok.status_code == 200, ok.text
    assert ok.json()["verified"] is True and ok.json()["business_code"]
    again = await asyncio.to_thread(client.post, "/v1/public/auth/verify-email", json={"token": raw})
    assert (again.status_code, again.json()) == (bad.status_code, bad.json())

    unknown = await asyncio.to_thread(client.post, "/v1/public/auth/resend-verification",
                                      json={"email": f"ghost-{uuid.uuid4().hex[:6]}@example.com"})
    known = await asyncio.to_thread(client.post, "/v1/public/auth/resend-verification",
                                    json={"email": body["owner_email"]})
    assert unknown.status_code == known.status_code == 202 and unknown.json() == known.json()


async def test_http_public_auth_routes_validate_their_json_body(client):
    for path in ("/v1/public/auth/verify-email", "/v1/public/auth/resend-verification"):
        r = await asyncio.to_thread(client.post, path, json={})
        assert r.status_code == 422, path
        locs = [tuple(e["loc"]) for e in r.json()["error"]["details"]["errors"]]
        assert ("query", "body") not in locs and any(loc[0] == "body" for loc in locs)
