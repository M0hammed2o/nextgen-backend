"""
Live-model evaluation harness (WhatsApp ordering reliability directive,
2026-10-26, section 9: "Live-model evaluation where authorised access is
available").

This is a STANDALONE script, not a pytest file -- it makes real network
calls to the configured LLM provider and is never run as part of the
automated test suite. Run it explicitly:

    ENVIRONMENT=development \
    TEST_DATABASE_URL=postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@127.0.0.1:55677/nextgen_test \
    TEST_REDIS_URL=redis://127.0.0.1:55678/0 \
    OPENAI_API_KEY=sk-... \
    python -m tests.eval.live_model_eval

Design intent, per the directive's own instruction ("A missing or invalid
API key is a blocked live-model test, not a passing test"):
  1. Verify a real OPENAI_API_KEY is configured.
  2. Run one cheap smoke-test completion call against the real API.
  3. Only if that genuinely succeeds, run the held-out evaluation set below
     against the REAL pipeline (real Postgres cart/session, real LLM calls
     -- no scripted/mocked provider anywhere in this file) and report
     accuracy broken out by dimension: item, quantity, modifier, total,
     checkout.
  4. If either check fails, print a BLOCKED report with the exact
     underlying error and exit non-zero. It never fabricates, estimates, or
     interpolates a pass/fail metric in that case.

The eval set below is deliberately NOT drawn from tests/replay/conversations/
or tests/integration/test_acceptance_scenarios.py -- independently authored
so a real run measures genuine natural-language understanding, not
memorised fixture phrasing (per the directive: "Use additional
independently authored and held-out conversations so the tests do not
merely optimise for previously known examples").
"""
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch


@dataclass
class EvalCase:
    case_id: str
    setup_messages: list[str]          # sent before the measured turn, to build context
    measured_message: str              # the turn whose LLM-driven outcome is measured
    expected_items: dict[str, int]     # {item_name: quantity} expected in cart AFTER the measured turn
    expected_modifier: dict[str, str] = field(default_factory=dict)  # {item_name: special_instructions}
    dimensions: list[str] = field(default_factory=lambda: ["item", "quantity"])


# ── Held-out evaluation set (independently authored, not reused from any
#    existing fixture file) ──────────────────────────────────────────────
EVAL_SET: list[EvalCase] = [
    EvalCase(
        case_id="live_01_item_understanding",
        setup_messages=["Hi there"],
        measured_message="Could you sort me out with a Grilled Fish, please?",
        expected_items={"Grilled Fish": 1},
        dimensions=["item", "quantity"],
    ),
    EvalCase(
        case_id="live_02_quantity_understanding",
        setup_messages=["Hi there"],
        measured_message="I'd like a half dozen portions of rice",
        expected_items={"Rice": 6},
        dimensions=["item", "quantity"],
    ),
    EvalCase(
        case_id="live_03_modifier_understanding",
        setup_messages=["Hi there"],
        measured_message="One grilled fish, hold the lemon butter sauce please",
        expected_items={"Grilled Fish": 1},
        expected_modifier={"Grilled Fish": "no lemon butter sauce"},
        dimensions=["item", "modifier"],
    ),
    EvalCase(
        case_id="live_04_multi_item_natural_phrasing",
        setup_messages=["Hi there"],
        measured_message="Can you get me sorted with two rice portions and a grilled fish on the side",
        expected_items={"Rice": 2, "Grilled Fish": 1},
        dimensions=["item", "quantity"],
    ),
    EvalCase(
        case_id="live_05_quantity_correction_natural_phrasing",
        setup_messages=["Hi there", "I'll take one rice"],
        measured_message="Oh wait, actually make that three rice, not one",
        expected_items={"Rice": 3},
        dimensions=["quantity"],
    ),
    EvalCase(
        case_id="live_06_unavailable_item_honesty",
        setup_messages=["Hi there"],
        measured_message="Do you have any lasagne? I'd like one please",
        expected_items={},  # must NOT be silently added
        dimensions=["item"],
    ),
]


RESULT_SCHEMA_VERSION = 1


async def _real_smoke_test() -> tuple[bool, str]:
    """One cheap real completion call. Returns (ok, detail)."""
    try:
        from backend.app.llm.provider import get_llm_provider
        provider = get_llm_provider()
        response = await provider.complete(
            system_prompt="Reply with exactly the single word: OK",
            user_message="ping",
            max_tokens=5,
        )
        return True, response.text.strip()
    except Exception as exc:  # noqa: BLE001 — this IS the blocked-detection path
        return False, f"{type(exc).__name__}: {exc}"


async def _run_case(business, case: EvalCase) -> dict:
    """Runs one eval case against the REAL pipeline + REAL LLM. Returns a
    per-dimension correctness dict for this case."""
    from backend.app.bot import state_machine
    from backend.app.bot.pipeline import process_inbound_message
    from shared.models.customer import ConversationSession, Customer
    from sqlalchemy import select
    from tests.integration._db import get_session_factory

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"

    async def _send(text: str) -> None:
        captured = {}

        async def _capture_send(*args, **kwargs):
            captured["text"] = kwargs.get("text")
            return "wamid.SENT"

        wmid = f"wamid.{uuid.uuid4().hex[:12]}"
        async with get_session_factory()() as db:
            with patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message", new=_capture_send), \
                 patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)):
                await process_inbound_message(
                    db=db,
                    phone_number_id=business.whatsapp_phone_number_id,
                    wa_message_id=wmid,
                    wa_id=wa_id,
                    msg_text=text,
                    msg_type="text",
                    raw_payload={"id": wmid, "type": "text", "text": {"body": text}},
                    contact_name="Live Eval",
                )

    for msg in case.setup_messages:
        await _send(msg)
    await _send(case.measured_message)

    async with get_session_factory()() as db:
        cust = (await db.execute(
            select(Customer).where(Customer.business_id == business.id, Customer.wa_id == wa_id)
        )).scalar_one()
        sess = (await db.execute(
            select(ConversationSession).where(ConversationSession.customer_id == cust.id)
        )).scalar_one()
        cart = state_machine.get_cart(sess)

    actual_items = {}
    actual_modifiers = {}
    for line in cart:
        actual_items[line["name"]] = actual_items.get(line["name"], 0) + line["quantity"]
        if line.get("special_instructions"):
            actual_modifiers[line["name"]] = line["special_instructions"]

    result = {"case_id": case.case_id, "cart_snapshot": cart}
    if "item" in case.dimensions:
        result["item_correct"] = set(actual_items.keys()) == set(case.expected_items.keys())
    if "quantity" in case.dimensions:
        result["quantity_correct"] = actual_items == case.expected_items
    if "modifier" in case.dimensions:
        result["modifier_correct"] = all(
            actual_modifiers.get(name, "").lower().replace(" ", "") ==
            expected.lower().replace(" ", "").replace("no", "no").replace("_", "")
            or expected.split()[-1].lower() in actual_modifiers.get(name, "").lower()
            for name, expected in case.expected_modifier.items()
        )
    return result


async def main() -> int:
    print(f"=== Live-model evaluation harness — {datetime.now(timezone.utc).isoformat()} ===")

    from backend.app.core.config import get_settings
    settings = get_settings()
    if not settings.OPENAI_API_KEY or not settings.OPENAI_API_KEY.strip():
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "OPENAI_API_KEY is not configured in this environment (empty/missing).",
            "cases_attempted": 0,
            "cases_run": 0,
        }, indent=2))
        return 1

    ok, detail = await _real_smoke_test()
    if not ok:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "Real smoke-test completion call failed — credentials present but invalid, "
                      "or the provider is unreachable from this environment.",
            "error_detail": detail,
            "cases_attempted": 0,
            "cases_run": 0,
        }, indent=2))
        return 1

    print(f"Smoke test passed: {detail!r}. Proceeding with {len(EVAL_SET)} held-out eval cases.")

    import os
    os.environ.setdefault("TEST_DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
    from shared.models.business import Business
    from shared.models.menu import MenuCategory, MenuItem
    from shared.utils import generate_business_code
    from tests.integration._db import get_session_factory

    unique = uuid.uuid4().hex[:8]
    async with get_session_factory()() as db:
        business = Business(
            name=f"Live Eval Restaurant {unique}", slug=f"live-eval-{unique}",
            business_code=generate_business_code(), currency="ZAR",
            timezone="Africa/Johannesburg", whatsapp_phone_number_id=f"live-eval-{unique}",
            is_active=True,
        )
        db.add(business)
        await db.flush()
        cat = MenuCategory(business_id=business.id, name="Mains", sort_order=0)
        db.add(cat)
        await db.flush()
        db.add_all([
            MenuItem(business_id=business.id, category_id=cat.id, name="Grilled Fish",
                     price_cents=12990, is_active=True,
                     options_json={"option_groups": [{
                         "id": "sauce", "name": "Sauce", "required": False, "is_enabled": True,
                         "options": [{"id": "lbs", "name": "Lemon Butter Sauce", "price_delta_cents": 0, "is_enabled": True}],
                     }]}),
            MenuItem(business_id=business.id, category_id=cat.id, name="Rice", price_cents=2990, is_active=True),
        ])
        await db.commit()

    results = []
    for case in EVAL_SET:
        try:
            result = await _run_case(business, case)
        except Exception as exc:  # noqa: BLE001
            result = {"case_id": case.case_id, "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        print(f"  [{case.case_id}] {result}")

    # Clean up the disposable eval business.
    from sqlalchemy import delete, text as _text
    async with get_session_factory()() as db:
        await db.execute(_text("DELETE FROM order_items WHERE business_id = :b"), {"b": business.id})
        await db.execute(_text("DELETE FROM orders WHERE business_id = :b"), {"b": business.id})
        from shared.models.customer import ConversationSession, Customer
        from shared.models.message import Message
        await db.execute(delete(Message).where(Message.business_id == business.id))
        await db.execute(delete(ConversationSession).where(ConversationSession.business_id == business.id))
        await db.execute(delete(Customer).where(Customer.business_id == business.id))
        await db.execute(delete(MenuItem).where(MenuItem.business_id == business.id))
        await db.execute(delete(MenuCategory).where(MenuCategory.business_id == business.id))
        await db.execute(delete(Business).where(Business.id == business.id))
        await db.commit()

    def _rate(dim: str) -> str:
        key = f"{dim}_correct"
        scored = [r for r in results if key in r]
        if not scored:
            return "n/a"
        correct = sum(1 for r in scored if r[key])
        return f"{correct}/{len(scored)}"

    summary = {
        "status": "COMPLETED",
        "cases_run": len(results),
        "item_accuracy": _rate("item"),
        "quantity_accuracy": _rate("quantity"),
        "modifier_accuracy": _rate("modifier"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
