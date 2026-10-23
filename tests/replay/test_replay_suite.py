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

from tests.replay.fixtures import SA_MENU, FakeSpecial, make_business, make_customer
from tests.replay.framework import ReplayRunner

# ── Conversation discovery ────────────────────────────────────────────────────

CONVERSATIONS_DIR = Path(__file__).parent / "conversations"


def _load_conversations() -> list[pytest.param]:
    params = []
    # Load hand-crafted conversations from root, then synthetic from subdirectories.
    # Sorted within each group so the run order is deterministic.
    for path in sorted(CONVERSATIONS_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        category = data.get("category", "uncategorized")
        label = f"{data['id']} [{category}] {data.get('title', '')}"
        params.append(pytest.param(data, id=label))
    for path in sorted(CONVERSATIONS_DIR.glob("**/*.json")):
        if path.parent == CONVERSATIONS_DIR:
            continue  # already loaded above
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        category = data.get("category", "uncategorized")
        label = f"{data['id']} [{category}] {data.get('title', '')}"
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

    # Allow per-conversation specials; default to none (matches every
    # pre-existing conversation JSON, which has no "specials" key).
    specials = [FakeSpecial(**spec) for spec in conversation.get("specials", [])]

    # Allow per-conversation multi-page menu-image pages; default to none
    # (matches every pre-existing conversation JSON, which has no
    # "menu_pages" key -- identical to a business that never adopted the
    # feature). Each entry: {"name": "...", "image_url": "..."}.
    menu_pages = conversation.get("menu_pages", [])

    runner = ReplayRunner(
        conv=conversation,
        business=business,
        customer=customer,
        menu_items=menu_items,
        specials=specials,
        menu_pages=menu_pages,
    )
    await runner.run()
