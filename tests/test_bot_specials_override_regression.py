"""
Regression suite for the production bug: WhatsApp customers asking about the
menu, specific items, prices, hours, or dietary questions kept getting
answered about SPECIALS instead, regardless of what they actually asked.

Root causes fixed (see KNOWN_BUGS.md / WORKLOG.md for the full writeup):

1. backend/app/bot/intent_router.py — the MENU_REQUEST keyword pattern's
   broad "what ... have/sell/serve" catch-all also matched specials-phrased
   questions ("What specials do you have?"), so a SPECIALS-worded question
   could be misclassified as MENU_REQUEST depending on exact phrasing
   (specials keyword patterns were checked AFTER the menu catch-all). Fixed
   by checking the explicit specials/deal/promo/discount keywords first.

2. backend/app/bot/prompt_builder.py — build_system_prompt() unconditionally
   injected the full "TODAY'S SPECIALS" block into EVERY LLM call regardless
   of the customer's actual question (pipeline._handle_with_llm loads and
   passes specials on every call, not just specials-related ones), with no
   instruction telling the model not to proactively lead with specials. This
   is the generalizable mechanism behind "regardless of what they actually
   asked" for any free-form question that reaches the LLM (price lookups,
   dietary questions, etc.) — fixed by adding an explicit STRICT RULE and a
   "reference only" label on the specials block itself.

Hypothesis (c) — stale conversation-state carrying a "discussing specials"
flag — was investigated and ruled out: state_machine.py has no such flag,
and grepping the pipeline turned up no code path that sets one.

The live LLM's actual judgment (whether GPT genuinely stops over-mentioning
specials given the new instruction) is NOT verifiable from this suite — see
tests/replay's ScriptedLLMProvider note in TESTING.md: this suite proves the
*pipeline's* handling, not the live model's output quality. That limitation
is intentional and disclosed, not silently assumed away.
"""

import pytest

from backend.app.bot import intent_router as ir
from backend.app.bot import prompt_builder
from shared.enums import MessageIntent

from tests.replay.fixtures import FakeMenuItem, FakeSpecial, make_business, make_customer
from tests.replay.framework import ReplayRunner


# ── 1. Intent classification — the 9 required behaviors ─────────────────────
# These are checked directly against intent_router.match_intent(), which is
# the first thing every inbound message passes through (pipeline.py step 6b).
# "How much is the Margherita?" and "Do you have vegetarian options?" are
# expected to return None — that's correct: it means "send to the LLM",
# not "misclassified". Those two are proven to actually reach the LLM (and
# not get silently hijacked by a deterministic branch) by the replay
# conversations in tests/replay/conversations/specials_override_regression/.
@pytest.mark.parametrize(
    "message,expected_intent",
    [
        ("Hi", MessageIntent.GREETING),
        ("Menu", MessageIntent.MENU_REQUEST),
        ("Show me your pizzas", MessageIntent.MENU_REQUEST),
        ("What specials do you have?", MessageIntent.SPECIALS_REQUEST),
        ("How much is the Margherita?", None),
        ("What time do you close?", MessageIntent.HOURS_REQUEST),
        ("Do you have vegetarian options?", None),
        ("I want to order two pizzas", MessageIntent.ORDER_START),
    ],
)
def test_required_behavior_intent_classification(message, expected_intent):
    assert ir.match_intent(message) == expected_intent


def test_specials_question_is_never_misclassified_as_menu_request():
    """
    This is the exact failure this bug-fix session found and fixed: before
    the intent_router.py reorder, "What specials do you have?" matched the
    MENU_REQUEST pattern's broad "what ... have" catch-all (checked before
    the specials keywords), returning MENU_REQUEST instead of
    SPECIALS_REQUEST. Verified failing pre-fix via direct interpreter run
    during investigation (returned MessageIntent.MENU_REQUEST).
    """
    assert ir.match_intent("What specials do you have?") == MessageIntent.SPECIALS_REQUEST
    assert ir.match_intent("What specials do you have?") != MessageIntent.MENU_REQUEST


# ── 2. System-prompt anti-bias instruction (prompt_builder.py) ──────────────

def _build_prompt_with_specials():
    business = make_business({"business_hours": None})
    items = [FakeMenuItem("Margherita Pizza", 9000)]
    specials = [FakeSpecial(title="2-for-1 Burgers", description="Mondays only")]
    return prompt_builder.build_system_prompt(
        business=business,
        categories=[],
        menu_items=items,
        specials=specials,
        conversation_state="IDLE",
        cart=[],
    )


def test_system_prompt_instructs_llm_not_to_lead_with_specials():
    prompt = _build_prompt_with_specials()
    assert "never lead with them" in prompt.lower() or "never lead with" in prompt.lower()
    assert "specials" in prompt.lower()
    assert "reference only" in prompt.lower()


def test_system_prompt_omits_specials_block_when_no_specials_exist():
    business = make_business({"business_hours": None})
    items = [FakeMenuItem("Margherita Pizza", 9000)]
    prompt = prompt_builder.build_system_prompt(
        business=business,
        categories=[],
        menu_items=items,
        specials=[],
        conversation_state="IDLE",
        cart=[],
    )
    assert "TODAY'S SPECIALS" not in prompt


# ── 3. Cross-tenant isolation — one business's menu/specials must never ────
#     appear when answering for a different business.
#
# The real per-request scoping (pipeline._load_menu/_load_specials filtering
# by Business.id, itself resolved server-side from the WhatsApp
# phone_number_id, never from client input) was verified by reading
# backend/app/bot/pipeline.py:_load_menu/_load_specials directly. This test
# proves the pipeline plumbing that consumes that data keeps two businesses'
# data completely separate end-to-end (no shared/global state anywhere in
# the request-handling path).
@pytest.mark.asyncio
async def test_no_cross_tenant_menu_or_specials_leakage():
    business_a = make_business({"name": "Business A Eatery"})
    business_b = make_business({"name": "Business B Diner"})

    menu_a = [FakeMenuItem("Alpha Burger", 5000)]
    menu_b = [FakeMenuItem("Beta Noodles", 6000)]

    specials_a = [FakeSpecial(title="Alpha Special Deal", description="Only at A")]
    specials_b = [FakeSpecial(title="Beta Special Deal", description="Only at B")]

    conv = {"id": "cross_tenant_probe", "turns": [{"message": "Menu"}]}

    runner_a = ReplayRunner(conv, business_a, make_customer(), menu_a, specials=specials_a)
    await runner_a.run()
    response_a = runner_a.responses[0]

    runner_b = ReplayRunner(conv, business_b, make_customer(), menu_b, specials=specials_b)
    await runner_b.run()
    response_b = runner_b.responses[0]

    assert "Alpha Burger" in response_a
    assert "Beta Noodles" not in response_a
    assert "Beta Special Deal" not in response_a

    assert "Beta Noodles" in response_b
    assert "Alpha Burger" not in response_b
    assert "Alpha Special Deal" not in response_b

    # Specials requests must also stay isolated per business.
    conv_specials = {"id": "cross_tenant_specials_probe", "turns": [{"message": "specials"}]}

    runner_a2 = ReplayRunner(conv_specials, business_a, make_customer(), menu_a, specials=specials_a)
    await runner_a2.run()
    specials_response_a = runner_a2.responses[0]

    runner_b2 = ReplayRunner(conv_specials, business_b, make_customer(), menu_b, specials=specials_b)
    await runner_b2.run()
    specials_response_b = runner_b2.responses[0]

    assert "Alpha Special Deal" in specials_response_a
    assert "Beta Special Deal" not in specials_response_a
    assert "Beta Special Deal" in specials_response_b
    assert "Alpha Special Deal" not in specials_response_b
