"""
Real-Postgres proof for the fail-closed WhatsApp number-handover binding
(migration 0059, WORKLOG.md 2026-10-29 — "Smokin Joes WhatsApp number
reuse" investigation).

Scenario: two isolated synthetic tenants ("Alpha" and "Beta"). Alpha owns a
WhatsApp number. A message addressed to Alpha's number is accepted by the
webhook (snapshotting Alpha's business_id + routing generation into the job,
exactly as routes_webhook.py now does) but its ARQ job is deliberately
DELAYED — it is not actually run yet. While it sits queued, the number is
reassigned from Alpha to Beta (exactly what admin_api's update_business does:
clear Alpha's whatsapp_phone_number_id, set it on Beta, bump BOTH their
whatsapp_routing_generation — only Alpha's change is what matters for this
test, but mirroring the real handler's behaviour for both keeps the scenario
realistic). THEN the delayed job finally runs.

Required outcome: the delayed job must be refused — not processed under
Beta, not processed under Alpha either (its own snapshot is now stale
there too, since Alpha's generation moved on), and not responsible for any
new Customer/ConversationSession/Message row under either tenant. A message
that is genuinely new (correctly snapshotted AFTER the handover) must still
work normally under Beta.

Uses tests/integration/_db.py's get_session_factory() (TEST_DATABASE_URL),
same precedent as test_whatsapp_pipeline_concurrency.py / test_loyalty_concurrency.py.

  docker run -d --name nextgen-routing-gen-testdb \\
    -e POSTGRES_USER=nextgen -e POSTGRES_PASSWORD=routing_local_only \\
    -e POSTGRES_DB=nextgen_routing_test -p 55931:5432 postgres:16-alpine
  export TEST_DATABASE_URL="postgresql+asyncpg://nextgen:routing_local_only@localhost:55931/nextgen_routing_test"
  export DATABASE_URL="$TEST_DATABASE_URL"
  python -m alembic upgrade head
  python -m pytest tests/integration/test_whatsapp_number_handover_routing_generation.py -q
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from backend.app.bot.pipeline import process_inbound_message
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.message import Message
from shared.utils import generate_business_code
from tests.integration._db import get_session_factory


def _patches():
    return (
        patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message", new=AsyncMock(return_value="wamid.SENT")),
        patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)),
    )


async def _make_business(db, name_prefix: str) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"{name_prefix} {unique}", slug=f"{name_prefix.lower()}-{unique}",
        business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
        is_active=True,
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _cleanup_business(business_id: uuid.UUID) -> None:
    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(Message).where(Message.business_id == business_id))
        await cleanup_db.execute(delete(ConversationSession).where(ConversationSession.business_id == business_id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == business_id))
        await cleanup_db.execute(delete(Business).where(Business.id == business_id))
        await cleanup_db.commit()


async def test_delayed_arq_job_from_before_a_handover_is_refused_not_misattributed(db):
    """
    The core scenario: a message queued while Alpha owned the number, whose
    job is only executed AFTER the number moved to Beta. Must be refused —
    never processed under Beta (the message was never sent to them), and
    never processed under Alpha either (their own copy of the snapshot is
    now stale, since the reassignment bumped Alpha's generation too).
    """
    shared_number = f"handover-{uuid.uuid4().hex[:10]}"
    alpha = await _make_business(db, "Alpha")
    beta = await _make_business(db, "Beta")

    alpha.whatsapp_phone_number_id = shared_number
    db.add(alpha)
    await db.commit()
    await db.refresh(alpha)

    # ── Webhook-accept-time snapshot for the message, while Alpha owns the number ──
    snapshot_business_id = alpha.id
    snapshot_generation = alpha.whatsapp_routing_generation  # 1

    # ── The handover happens BEFORE the job runs (simulating the ARQ delay) ──
    # Mirrors the real admin procedure exactly: two SEQUENTIAL commits (clear
    # the old owner, THEN set the new owner) — never combined into one, since
    # the column is UNIQUE and a single flush setting both rows at once can
    # race against its own ordering (confirmed directly: an earlier version
    # of this test tried one combined commit and hit
    # uq_businesses_whatsapp_phone_number_id — this real constraint is
    # exactly why the admin procedure must be sequential, not simultaneous).
    alpha.whatsapp_phone_number_id = None
    alpha.whatsapp_routing_generation += 1
    db.add(alpha)
    await db.commit()

    beta.whatsapp_phone_number_id = shared_number
    beta.whatsapp_routing_generation += 1
    db.add(beta)
    await db.commit()

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"
    wa_message_id = f"wamid.delayed-{uuid.uuid4().hex[:12]}"
    delayed_text = "Yes please confirm my order"  # exactly the kind of mid-conversation message that must never land on the wrong tenant

    async with get_session_factory()() as job_db:
        p1, p2 = _patches()
        with p1 as mock_send, p2:
            await process_inbound_message(
                db=job_db,
                phone_number_id=shared_number,
                wa_message_id=wa_message_id,
                wa_id=wa_id,
                msg_text=delayed_text,
                msg_type="text",
                raw_payload={"id": wa_message_id, "type": "text", "text": {"body": delayed_text}},
                contact_name="Delayed Job Customer",
                expected_business_id=snapshot_business_id,
                expected_routing_generation=snapshot_generation,
            )
            mock_send.assert_not_called()  # no reply was ever sent for the refused message

    async with get_session_factory()() as verify_db:
        for business_id, label in ((alpha.id, "Alpha"), (beta.id, "Beta")):
            customers = (
                await verify_db.execute(
                    select(Customer).where(Customer.business_id == business_id, Customer.wa_id == wa_id)
                )
            ).scalars().all()
            assert customers == [], f"no customer must have been created under {label} for the refused message"

            messages = (
                await verify_db.execute(select(Message).where(Message.business_id == business_id))
            ).scalars().all()
            assert messages == [], f"no message row must exist under {label} for the refused delivery"

    await _cleanup_business(alpha.id)
    await _cleanup_business(beta.id)


async def test_message_correctly_snapshotted_after_handover_works_normally_under_new_owner(db):
    """
    Control case: a message that genuinely arrives (and is correctly
    snapshotted) AFTER the handover must process completely normally under
    the new owner — proving the fail-closed check doesn't block legitimate
    post-handover traffic, only the stale pre-handover window.
    """
    shared_number = f"handover-{uuid.uuid4().hex[:10]}"
    alpha = await _make_business(db, "Alpha")
    beta = await _make_business(db, "Beta")

    alpha.whatsapp_phone_number_id = shared_number
    db.add(alpha)
    await db.commit()

    # Handover happens — two sequential commits, matching the real admin
    # procedure (see the other test's comment for why this can't be combined).
    alpha.whatsapp_phone_number_id = None
    alpha.whatsapp_routing_generation += 1
    db.add(alpha)
    await db.commit()

    beta.whatsapp_phone_number_id = shared_number
    beta.whatsapp_routing_generation += 1
    db.add(beta)
    await db.commit()
    await db.refresh(beta)

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"
    wa_message_id = f"wamid.post-handover-{uuid.uuid4().hex[:12]}"

    async with get_session_factory()() as job_db:
        p1, p2 = _patches()
        with p1 as mock_send, p2:
            await process_inbound_message(
                db=job_db,
                phone_number_id=shared_number,
                wa_message_id=wa_message_id,
                wa_id=wa_id,
                msg_text="Hi",
                msg_type="text",
                raw_payload={"id": wa_message_id, "type": "text", "text": {"body": "Hi"}},
                contact_name="Post-Handover Customer",
                expected_business_id=beta.id,
                expected_routing_generation=beta.whatsapp_routing_generation,
            )
            mock_send.assert_called()  # a real greeting was sent — legitimate traffic works

    async with get_session_factory()() as verify_db:
        beta_customers = (
            await verify_db.execute(
                select(Customer).where(Customer.business_id == beta.id, Customer.wa_id == wa_id)
            )
        ).scalars().all()
        assert len(beta_customers) == 1, "the new owner must have correctly onboarded this customer"

        alpha_customers = (
            await verify_db.execute(
                select(Customer).where(Customer.business_id == alpha.id, Customer.wa_id == wa_id)
            )
        ).scalars().all()
        assert alpha_customers == [], "the old owner must see nothing from this post-handover message"

    await _cleanup_business(alpha.id)
    await _cleanup_business(beta.id)


async def test_legacy_caller_with_no_snapshot_falls_back_to_original_behaviour(db):
    """
    A caller that passes neither expected_business_id nor
    expected_routing_generation (any pre-0059 code path, or a test that
    hasn't been updated) must fall back to the exact original
    lookup-by-phone-number-only behaviour — this fix is additive, not a
    breaking change for anything that hasn't opted in to the snapshot.
    """
    alpha = await _make_business(db, "Legacy")
    unique_number = f"legacy-{uuid.uuid4().hex[:10]}"
    alpha.whatsapp_phone_number_id = unique_number
    db.add(alpha)
    await db.commit()

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"
    wa_message_id = f"wamid.legacy-{uuid.uuid4().hex[:12]}"

    async with get_session_factory()() as job_db:
        p1, p2 = _patches()
        with p1 as mock_send, p2:
            await process_inbound_message(
                db=job_db,
                phone_number_id=unique_number,
                wa_message_id=wa_message_id,
                wa_id=wa_id,
                msg_text="Hi",
                msg_type="text",
                raw_payload={"id": wa_message_id, "type": "text", "text": {"body": "Hi"}},
                contact_name="Legacy Caller",
                # expected_business_id / expected_routing_generation deliberately omitted
            )
            mock_send.assert_called()

    async with get_session_factory()() as verify_db:
        customers = (
            await verify_db.execute(
                select(Customer).where(Customer.business_id == alpha.id, Customer.wa_id == wa_id)
            )
        ).scalars().all()
        assert len(customers) == 1, "legacy callers (no snapshot) must still work exactly as before"

    await _cleanup_business(alpha.id)
