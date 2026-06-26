"""
Conversation Replay Suite — pytest entry point.

Discovers all *.json files under tests/replay/conversations/, loads each as
a conversation definition, and runs it through the ReplayRunner.

Adding a new conversation:
  1. Create tests/replay/conversations/conv_NNN.json
  2. Run: pytest tests/replay/ -v

CI integration:
  - Add `pytest tests/replay/` to the deployment pipeline.
  - A failing replay blocks the deploy.

Replay failure output:
  - Turn index, expected vs actual state / cart / response.
  - Full conversation ID and description in the test name.
"""

import json
from pathlib import Path

import pytest

from tests.replay.fixtures import SA_MENU, make_business, make_customer
from tests.replay.framework import ReplayRunner

# ── Conversation discovery ────────────────────────────────────────────────────

CONVERSATIONS_DIR = Path(__file__).parent / "conversations"


def _load_conversations() -> list[pytest.param]:
    params = []
    for path in sorted(CONVERSATIONS_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        label = f"{data['id']} — {data.get('title', '')}"
        params.append(pytest.param(data, id=label))
    return params


# ── Test ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("conversation", _load_conversations())
async def test_conversation_replay(conversation: dict) -> None:
    """
    Replay a complete customer conversation and verify every assertion.

    Failures include:
      - Wrong conversation state after any turn
      - Cart / confirmed_cart mismatch
      - Missing or unexpected words in bot response
      - Final DB order does not match expected items / totals / mode
    """
    business_spec = conversation.get("business", {})
    business = make_business(business_spec)
    customer = make_customer()

    # Allow per-conversation menu overrides; default to full SA_MENU
    menu_items = SA_MENU

    runner = ReplayRunner(
        conv=conversation,
        business=business,
        customer=customer,
        menu_items=menu_items,
    )
    await runner.run()
