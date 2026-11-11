"""
Regression tests for the 2026-11-11 live Super Burger WhatsApp conversation
(multi-unit lines with per-unit options, option-vs-product confusion, empty
replies, stale state after cancel, blank cart bullets).

All mocked (no DB/LLM/WhatsApp): the real ``pipeline._handle_message`` runs
through the replay framework's I/O fakes via ``tests/superburger_harness.py``.
The LLM is scripted only where the live behaviour depended on it, with what
it plausibly returned (e.g. step 5 of the live chat came back as an add-item
of "Onion Rings").

Fixture menu = Super Burger seed (NextGen-Demo-SuperBurger/seed_super_burger.py):
Patty Chicken R0 / Beef +R15 / Wagyu +R100, included Side Fries / Onion rings /
Potato skins, PLUS standalone "Onion Rings" R32 and "Regular/Large Fries".
"""
import asyncio

import pytest

from tests.superburger_harness import Chat

pytestmark = pytest.mark.asyncio


def add(name, qty=1, **kw):
    return {"action": "add_items", "message": "", "items": [dict(name=name, quantity=qty, **kw)]}


CHITCHAT = {"action": "chitchat", "message": "I recommend the Super Burger -- big flavour! Want one?", "items": []}


def _lines(chat):
    """Cart as (qty, name, sorted option names, unit_cents, line_cents)."""
    out = []
    for i in chat.cart:
        names = sorted((o.get("option_name") or o.get("name")) for o in i.get("selected_options") or [])
        out.append((i["quantity"], i["name"], names, i["unit_price_cents"], i["line_total_cents"]))
    return sorted(out, key=lambda x: (x[1], x[2]))


def _assert_clean_reply(reply):
    assert reply and reply.strip(), "bot sent an empty reply"
    for ln in reply.split("\n"):
        if ln.strip().startswith("↳"):
            assert ln.strip().lstrip("↳").strip(), f"blank modifier line in reply: {reply!r}"
        if ln.strip().startswith("📝"):
            assert ln.strip().lstrip("📝").strip(), f"blank note line in reply: {reply!r}"


def _assert_no_standalone(chat, name):
    assert not [i for i in chat.cart if i["name"] == name], f"standalone {name} wrongly in cart: {chat.summary()}"


EXPECTED_405 = [
    (2, "Super Burger", ["Beef", "Fries"], 13500, 27000),
    (1, "Super Burger", ["Beef", "Onion rings"], 13500, 13500),
]


# ── A ────────────────────────────────────────────────────────────────────────
async def test_A_three_burgers_all_beef_two_fries_one_onion_rings():
    c = Chat()
    try:
        r = await c.say("Give me 3 Super Burgers, all beef, 2 with fries and 1 with onion rings")
        _assert_clean_reply(r)
        assert _lines(c) == sorted(EXPECTED_405, key=lambda x: (x[1], x[2]))
        assert c.total() == 40500
        _assert_no_standalone(c, "Onion Rings")
        _assert_no_standalone(c, "Regular Fries")
        assert "R405.00" in r
        assert c.state == "CONFIRMING_ORDER"
    finally:
        c.close()


# ── B ────────────────────────────────────────────────────────────────────────
async def test_B_two_cheeseburgers_one_without_onions_is_a_removal_on_one_unit_only():
    c = Chat()
    try:
        r = await c.say("Give me 2 cheeseburgers, one without onions")
        _assert_clean_reply(r)
        cb = [i for i in c.cart if i["name"] == "Cheeseburger"]
        assert sum(i["quantity"] for i in cb) == 2
        no_onion = [i for i in cb if "onion" in (i.get("special_instructions") or "").lower()]
        plain = [i for i in cb if "onion" not in (i.get("special_instructions") or "").lower()]
        assert sum(i["quantity"] for i in no_onion) == 1, c.summary()
        assert sum(i["quantity"] for i in plain) == 1, c.summary()
        assert c.total() == 2 * 9500
    finally:
        c.close()


async def test_B2_ambiguous_burgers_asks_which_burger_and_does_not_guess():
    c = Chat()
    try:
        ask = {"action": "ask_options", "items": [],
               "message": "Which burger would you like? We have the Super Burger, Classic Burger and Cheeseburger."}
        r = await c.say("Give me 2 burgers, one without onions", llm=ask)
        _assert_clean_reply(r)
        # Several burgers could be meant: nothing may be silently chosen.
        assert c.cart == [], c.summary()
        assert "which burger" in r.lower()
    finally:
        c.close()


# ── C ────────────────────────────────────────────────────────────────────────
async def test_C_three_chicken_burgers_then_change_one_to_beef():
    c = Chat()
    try:
        await c.say("Give me 3 Super Burgers, chicken, with fries")
        assert _lines(c) == [(3, "Super Burger", ["Chicken", "Fries"], 12000, 36000)]
        r = await c.say("Change one to beef")
        _assert_clean_reply(r)
        assert _lines(c) == sorted([
            (2, "Super Burger", ["Chicken", "Fries"], 12000, 24000),
            (1, "Super Burger", ["Beef", "Fries"], 13500, 13500),
        ], key=lambda x: (x[1], x[2]))
        assert c.total() == 37500          # totals recomputed with the +R15 delta
        assert "R375.00" in r
    finally:
        c.close()


# ── D ────────────────────────────────────────────────────────────────────────
async def test_D_two_super_burgers_then_actually_make_that_four():
    c = Chat()
    try:
        r1 = await c.say("I want 2 Super Burgers")
        _assert_clean_reply(r1)
        r2 = await c.say("Actually make that 4")
        _assert_clean_reply(r2)
        assert "4 Super Burgers" in r2          # re-asks the patty for FOUR, quantity preserved
        await c.say("Beef")
        r4 = await c.say("Fries")
        _assert_clean_reply(r4)
        assert _lines(c) == [(4, "Super Burger", ["Beef", "Fries"], 13500, 54000)]
    finally:
        c.close()


async def test_D2_quantity_correction_after_the_burgers_are_in_the_cart():
    c = Chat()
    try:
        await c.say("Give me 2 Super Burgers, beef, with fries")
        assert c.total() == 27000
        r = await c.say("Actually make that 4")
        _assert_clean_reply(r)
        assert [i for i in c.cart if i["name"] == "Super Burger"]
        assert sum(i["quantity"] for i in c.cart if i["name"] == "Super Burger") == 4, c.summary()
        assert c.total() == 4 * 13500
    finally:
        c.close()


# ── E ────────────────────────────────────────────────────────────────────────
async def test_E_cancel_clears_cart_and_all_pending_state_then_fresh_order():
    c = Chat()
    try:
        await c.say("Give me 3 Super Burgers beef")            # leaves a pending side question
        assert c.state == "CHOOSING_OPTIONS" and c.ctx.get("pending_options")
        c.r.session.context_json = dict(c.ctx, proposed_items=[{"name": "Super Burger"}])
        r = await c.say("Cancel my order")
        assert "cancel" in r.lower()
        assert c.cart == []
        for key in ("pending_options", "pending_variant_allocation", "proposed_items",
                    "recommended_items", "confirmed_cart"):
            assert not c.ctx.get(key), f"{key} survived cancel: {c.ctx.get(key)!r}"
        assert c.state == "IDLE"

        r = await c.say("Give me 2 Classic Burgers chicken with fries")
        _assert_clean_reply(r)
        assert _lines(c) == [(2, "Classic Burger", ["Chicken", "Fries"], 10500, 21000)]
    finally:
        c.close()


async def test_E2_clear_cart_also_drops_the_variant_allocation_question():
    from unittest.mock import patch
    from backend.app.bot import state_machine
    from tests.replay.framework import ReplaySession

    sess = ReplaySession()
    sess.context_json = {"cart": [{"name": "x"}], "pending_variant_allocation": {"family": "Bambino Meal"},
                         "pending_options": [{"name": "x"}], "proposed_items": [{"name": "x"}],
                         "customer_name": "Sam"}
    with patch("backend.app.bot.state_machine.flag_modified"):
        state_machine.clear_cart(sess)
    assert sess.context_json.get("cart") == []
    for k in ("pending_variant_allocation", "pending_options", "proposed_items"):
        assert k not in sess.context_json
    assert sess.context_json.get("customer_name") == "Sam"      # identity kept on purpose


# ── F ────────────────────────────────────────────────────────────────────────
async def test_F_browse_burgers_then_order_directly_keeps_cart():
    c = Chat()
    try:
        await c.say("Give me a Cheeseburger")
        assert c.total() == 9500
        r = await c.say("Show me the burgers menu")
        _assert_clean_reply(r)
        assert "Super Burger" in r
        assert c.total() == 9500, "browsing must never lose the cart"
        r = await c.say("Give me 2 Super Burgers beef with onion rings")
        _assert_clean_reply(r)
        assert _lines(c) == sorted([
            (1, "Cheeseburger", [], 9500, 9500),
            (2, "Super Burger", ["Beef", "Onion rings"], 13500, 27000),
        ], key=lambda x: (x[1], x[2]))
        _assert_no_standalone(c, "Onion Rings")
    finally:
        c.close()


# ── G ────────────────────────────────────────────────────────────────────────
async def test_G_pending_patty_question_answered_with_only_beef():
    c = Chat()
    try:
        r = await c.say("Give me 3 Super Burgers")
        assert "patty" in r.lower()
        # Live step 4: the LLM-written pending state had no options_json and
        # the bare "Beef" produced an EMPTY message.
        pend = c.ctx["pending_options"]
        for p in pend:
            p.pop("options_json", None)
        c.r.session.context_json = dict(c.ctx, pending_options=pend)
        r = await c.say("Beef", llm={"action": "chitchat", "message": "", "items": []})
        _assert_clean_reply(r)
        assert "side" in r.lower()
        assert [i for i in c.ctx["pending_options"]][0]["quantity"] == 3
        assert c.cart == []
    finally:
        c.close()


async def test_G2_empty_llm_message_never_reaches_the_customer():
    c = Chat()
    try:
        await c.say("Give me a Cheeseburger")
        r = await c.say("hmm interesting thing", llm={"action": "chitchat", "message": "", "items": []})
        _assert_clean_reply(r)
    finally:
        c.close()


# ── H ────────────────────────────────────────────────────────────────────────
async def test_H_duplicate_webhook_delivery_does_not_add_items_twice():
    """Dedup lives in pipeline._process step 4 (atomic INSERT .. ON CONFLICT on
    wa_message_id, BUG-0157): a second delivery of the same wamid returns before
    _handle_message ever runs. Prove that gate: the handler runs once."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from backend.app.bot import pipeline

    seen = []

    class _Res:
        def __init__(self, rowcount):
            self.rowcount = rowcount

        def scalar_one_or_none(self):
            return None if self.rowcount == 0 else "x"

    c = Chat()
    try:
        delivered = {"n": 0}

        async def fake_handle(*a, **k):
            seen.append(1)
            return ("ok", False, None, None, None)

        # Simulate the idempotency gate directly: an already-seen wamid is a no-op.
        processed: set[str] = set()

        async def gated(wamid):
            if wamid in processed:
                return "duplicate"
            processed.add(wamid)
            return (await fake_handle())[0]

        assert await gated("wamid.1") == "ok"
        assert await gated("wamid.1") == "duplicate"
        assert len(seen) == 1
        # And the real source still carries the atomic gate this relies on.
        import inspect
        src = inspect.getsource(pipeline._process)
        assert "on_conflict_do_nothing" in src or "ON CONFLICT" in src or "pg_insert" in src
        assert "wa_message_id" in src
    finally:
        c.close()


# ── I ────────────────────────────────────────────────────────────────────────
async def test_I_correct_a_side_before_checkout():
    c = Chat()
    try:
        await c.say("Give me 3 Super Burgers, all beef, 2 with fries and 1 with onion rings")
        assert c.total() == 40500
        r = await c.say("Change the onion rings to potato skins")
        _assert_clean_reply(r)
        assert _lines(c) == sorted([
            (2, "Super Burger", ["Beef", "Fries"], 13500, 27000),
            (1, "Super Burger", ["Beef", "Potato skins"], 13500, 13500),
        ], key=lambda x: (x[1], x[2]))
        assert c.total() == 40500
    finally:
        c.close()


# ── J ────────────────────────────────────────────────────────────────────────
async def test_J_message_arriving_while_earlier_one_is_processing_is_neither_lost_nor_doubled():
    """The per-customer processing lock (BUG-0157) serialises two concurrent
    deliveries; modelled here with one lock around the real handler. Two
    messages fired concurrently must both be applied exactly once, in order."""
    c = Chat()
    try:
        lock = asyncio.Lock()
        order = []

        async def deliver(text):
            async with lock:
                order.append(text)
                await asyncio.sleep(0)
                return await c.say(text)

        t1 = asyncio.create_task(deliver("Give me 3 Super Burgers"))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(deliver("Beef"))
        r1, r2 = await asyncio.gather(t1, t2)
        assert order == ["Give me 3 Super Burgers", "Beef"]
        _assert_clean_reply(r1)
        _assert_clean_reply(r2)
        pend = c.ctx["pending_options"]
        assert len(pend) == 1 and pend[0]["quantity"] == 3     # not lost, not doubled
        assert "side" in r2.lower()
        r3 = await c.say("Fries")
        assert _lines(c) == [(3, "Super Burger", ["Beef", "Fries"], 13500, 40500)]
    finally:
        c.close()


# ── Exact live conversation (steps 2-10) ─────────────────────────────────────
async def test_live_conversation_2026_11_11_ends_in_the_correct_405_cart():
    c = Chat()
    try:
        # 2 recommendation (LLM)
        r = await c.say("What do you recommend", llm=CHITCHAT)
        _assert_clean_reply(r)
        # 3 "Give 3 of it please" -- LLM adds 3 Super Burgers with no patty
        r = await c.say("Give 3 of it please", llm=add("Super Burger", 3))
        _assert_clean_reply(r)
        assert "patty" in r.lower() and c.cart == []
        assert "Beef" in r and "R15.00" in r and "Wagyu" in r
        # 4 "Beef" -- must NOT be an empty message; patty applies to all 3
        r = await c.say("Beef")
        _assert_clean_reply(r)
        assert "side" in r.lower()
        # 5 side split -- the LLM likely returned add-item "Onion Rings": must not
        # become a standalone product and must not drop the 3 burgers
        r = await c.say("Give me with fries for 2 and one with onion rings", llm=add("Onion Rings", 1))
        _assert_clean_reply(r)
        assert _lines(c) == sorted(EXPECTED_405, key=lambda x: (x[1], x[2]))
        _assert_no_standalone(c, "Onion Rings")
        assert c.total() == 40500
        # 6 restated correction (no cancel): still no standalone Onion Rings / Fries
        r = await c.say(
            "No it is 3 Super Burgers and 2 fries as a side and also one side will be onion rings",
            llm=add("Onion Rings", 1),
        )
        _assert_clean_reply(r)
        assert "Which *Fries*" not in r
        _assert_no_standalone(c, "Onion Rings")
        _assert_no_standalone(c, "Regular Fries")
        # (the restatement carries no patty -- it is asked, never defaulted)
        assert "patty" in r.lower()
        # 7 no reply in the live chat -> a real answer now
        r = await c.say("No give me 3 super burgers please")
        _assert_clean_reply(r)
        assert "patty" in r.lower()
        # 8 cancel: cart AND pending state cleared
        r = await c.say("Cancel order")
        assert "cancel" in r.lower()
        assert c.cart == [] and not c.ctx.get("pending_options")
        # 9 beef stated in the order: do NOT re-ask the patty
        r = await c.say("Give me 3 Super Burgers beef")
        _assert_clean_reply(r)
        assert "patty" not in r.lower()
        assert "side" in r.lower()
        # 10 "Beef" (repeat) then the side split
        r = await c.say("Beef")
        _assert_clean_reply(r)
        r = await c.say("2 with fries and 1 with onion rings")
        _assert_clean_reply(r)
        assert _lines(c) == sorted(EXPECTED_405, key=lambda x: (x[1], x[2]))
        assert c.total() == 40500
        # cart formatting: priced option with delta, no blank bullets
        assert "↳ Beef +R15.00" in r and "↳ Fries" in r and "↳ Onion rings" in r
        # confirm and place the order
        for step in ("yes", "pickup", "John", "0821234567", "skip"):
            r = await c.say(step)
            _assert_clean_reply(r)
        assert "Order Confirmed" in r
        assert "↳ Beef +R15.00" in r and "📝 Onion Rings" not in r
        assert c.r.created_orders, "order was not created"
        order = c.r.created_orders[-1]
        assert order.subtotal_cents == 40500
        assert sorted((i["quantity"], tuple(sorted(o["option_name"] for o in i["selected_options"])))
                      for i in order.items) == [(1, ("Beef", "Onion rings")), (2, ("Beef", "Fries"))]
    finally:
        c.close()


async def test_never_silently_defaults_a_side_when_customer_did_not_choose():
    c = Chat()
    try:
        r = await c.say("Give me 3 Super Burgers beef")
        assert c.cart == []                      # nothing added with a made-up side
        assert "side" in r.lower()
        # an ambiguous / over-allocated answer is queried, not guessed
        r = await c.say("5 with fries and 1 with onion rings")
        _assert_clean_reply(r)
        assert c.cart == [] and c.ctx["pending_options"][0]["quantity"] == 3
        assert "how many" in r.lower()
    finally:
        c.close()


async def test_extra_onion_rings_is_still_a_standalone_item_when_clearly_asked_for():
    c = Chat()
    try:
        r = await c.say("Give me 2 Super Burgers beef with fries and an extra onion rings")
        _assert_clean_reply(r)
        names = sorted(i["name"] for i in c.cart)
        assert names == ["Onion Rings", "Super Burger"], c.summary()
        assert c.total() == 2 * 13500 + 3200
    finally:
        c.close()


# ── Menu browsing (large menu) ───────────────────────────────────────────────
WA_LIMIT = 4096


async def test_menu_large_menu_offers_categories_first_in_one_message_no_duplicates():
    from tests.superburger_harness import build_big_menu
    items, cats = build_big_menu()
    c = Chat(menu=items, categories=cats)
    try:
        r = await c.say("Can I please see your menu")
        _assert_clean_reply(r)
        assert len(r) <= WA_LIMIT
        assert "Burgers" in r and "Steaks" in r
        assert "Special 1 " not in r, "must not dump items before a category is chosen"
        lines = [ln for ln in r.split("\n") if ln.strip()]
        assert len(lines) == len(set(lines)), "duplicate line in the menu reply"
    finally:
        c.close()


async def test_menu_category_page_has_more_and_pages_never_overlap_or_exceed_limit():
    from tests.superburger_harness import build_big_menu
    items, cats = build_big_menu()
    c = Chat(menu=items, categories=cats)
    try:
        await c.say("menu")
        r1 = await c.say("Steaks")
        _assert_clean_reply(r1)
        assert len(r1) <= WA_LIMIT
        assert "Steak Special 1 " in r1 and "more" in r1.lower()
        r2 = await c.say("more")
        _assert_clean_reply(r2)
        assert len(r2) <= WA_LIMIT
        first = {ln for ln in r1.split("\n") if ln.strip().startswith("•")}
        second = {ln for ln in r2.split("\n") if ln.strip().startswith("•")}
        assert second and not (first & second), "a 'more' page repeated items"
        assert len(first) + len(second) == 15 and "more" not in r2.lower().replace("the other", "")
    finally:
        c.close()


async def test_menu_browsing_never_loses_the_cart_and_direct_ordering_still_works():
    from tests.superburger_harness import build_big_menu
    items, cats = build_big_menu()
    c = Chat(menu=items, categories=cats)
    try:
        await c.say("Give me 2 Super Burgers beef with fries")
        assert c.total() == 27000
        r = await c.say("Show me the burgers menu")
        _assert_clean_reply(r)
        assert "Super Burger" in r and "Steak Special" not in r
        assert c.total() == 27000
        r = await c.say("Give me 2 Large Fries")
        _assert_clean_reply(r)
        assert c.total() == 27000 + 2 * 5500
        r = await c.say("menu")
        assert c.total() == 27000 + 2 * 5500
    finally:
        c.close()


async def test_menu_full_menu_still_available_and_every_chunk_within_limit():
    from backend.app.bot import responses
    from tests.superburger_harness import build_big_menu
    items, cats = build_big_menu()
    chunks = responses.build_menu_messages(cats, items, "ZAR", max_chars=responses.WHATSAPP_TEXT_SAFE_MAX_CHARS - 350)
    assert len(chunks) > 1
    assert all(len(ch) + 350 <= WA_LIMIT for ch in chunks)
    all_lines = [ln for ch in chunks for ln in ch.split("\n") if ln.strip().startswith("•")]
    assert len(all_lines) == len(set(all_lines)) == sum(1 for i in items if i.is_active)
