"""
Deterministic edits to cart lines that already exist (2026-11-11 incident):

  * option change  -- "change one to beef", "make them all wagyu",
                      "change the onion rings to potato skins"
  * total quantity -- "actually make that 4"

Pure planning + a small cart mutator; no DB/LLM. The pipeline calls
``plan_edit`` and either applies the plan or asks the clarifying question the
plan carries. Nothing is guessed: when the target line/option/count is not
unambiguous the plan is a question, never a mutation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.app.bot import option_allocation as oa

_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM_ALT = r"\d{1,2}|" + "|".join(_NUMS)

_CHANGE_VERB_RE = re.compile(r"\b(change|switch|swap|make|turn|replace|set)\b", re.I)
_ALL_RE = re.compile(r"\b(all|both|every|each|them|everything)\b", re.I)
_TOTAL_QTY_RE = re.compile(
    r"^\s*(?:no[,.!\s]+)?(?:(?:actually|ok(?:ay)?|sorry|please|hmm+|wait)[,\s]+)*"
    r"(?:(?:make|change|set|update|let'?s\s+(?:do|make))\s+(?:that|it|them|this|the\s+order|the\s+quantity)\s*(?:to|into)?\s*"
    r"|(?:make|change|set|update)\s+(?:that|it|them|this)?\s*(?:to|into)\s*"
    r"|actually\s+)"
    r"(" + _NUM_ALT + r")\b\s*(?:please|thanks|thank\s+you)?\s*[.!]?\s*$",
    re.I,
)


@dataclass
class EditPlan:
    kind: str                       # "option_change" | "set_quantity" | "ask"
    question: str | None = None
    # option_change
    moves: list[tuple[int, int]] = field(default_factory=list)   # (cart_index, units)
    group: dict | None = None
    target: dict | None = None
    # set_quantity
    cart_index: int | None = None
    new_qty: int | None = None


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUMS[tok.lower()]


def _item_groups(menu_by_id: dict, line: dict) -> list[dict]:
    mi = menu_by_id.get(str(line.get("menu_item_id")))
    oj = getattr(mi, "options_json", None) or {}
    return [g for g in (oj.get("option_groups") or []) if g.get("is_enabled", True)]


def plan_edit(text: str, cart: list[dict], menu_items: list) -> EditPlan | None:
    if not cart:
        return None
    low = text.lower().strip()
    menu_by_id = {str(i.id): i for i in menu_items}

    # ── total quantity: "actually make that 4" ───────────────────────────────
    m = _TOTAL_QTY_RE.match(low)
    if m:
        new_qty = _num(m.group(1))
        if new_qty < 1 or new_qty > 20:
            return None
        last_mid = cart[-1]["menu_item_id"]
        idxs = [i for i, ln in enumerate(cart) if ln["menu_item_id"] == last_mid]
        name = cart[-1]["name"]
        if len(idxs) == 1:
            if cart[idxs[0]]["quantity"] == new_qty:
                return None
            return EditPlan("set_quantity", cart_index=idxs[0], new_qty=new_qty)
        total = sum(cart[i]["quantity"] for i in idxs)
        return EditPlan(
            "ask",
            question=(
                f"You currently have {total} {name}s with different options. "
                f"Which one should I change to make it {new_qty}? "
                f"(for example \"add {max(new_qty - total, 1)} more with <option>\")"
            ),
        )

    # ── option change ────────────────────────────────────────────────────────
    if not _CHANGE_VERB_RE.search(low):
        return None
    # candidate option mentions across every cart line's option groups
    mentions: list[tuple[dict, dict, int, int]] = []   # (group, option, start, end)
    seen_ids: set = set()
    for line in cart:
        for g in _item_groups(menu_by_id, line):
            if (g.get("id"), line["menu_item_id"]) in seen_ids:
                continue
            seen_ids.add((g.get("id"), line["menu_item_id"]))
            for mn in oa.find_mentions(oa.enabled_options(g), low):
                mentions.append((g, mn.option, mn.start, mn.end))
    if not mentions:
        return None
    # de-duplicate identical (group id, option id, position)
    uniq = {}
    for g, o, s_, e_ in mentions:
        uniq[(g.get("id"), o.get("id"), s_)] = (g, o, s_, e_)
    mentions = sorted(uniq.values(), key=lambda x: x[2])
    # the change must be phrased "... to/into/with/make <option>"
    to_m = re.search(r"\b(?:to|into|with)\b|\bmake\s+(?:it|them|that|one|" + _NUM_ALT + r")\b", low)
    if not to_m:
        return None
    after = [x for x in mentions if x[2] >= to_m.start()]
    if not after:
        return None
    group, target, _s, _e = after[-1] if len(after) == 1 else after[-1]
    before = [x for x in mentions if x[3] <= to_m.start() and x[0].get("id") == group.get("id")]
    source = before[-1][1] if before else None

    # who/how many: number word before "to" (or after "make")
    head = low[: to_m.start()] if to_m.group(0) in ("to", "into", "with") else low[: to_m.end()]
    cnt = None
    nm = None
    for nm in re.finditer(r"\b(" + _NUM_ALT + r")\b", head):
        cnt = _num(nm.group(1))
    wants_all = bool(_ALL_RE.search(head)) and cnt is None

    # candidate lines: carry this group, don't already have the target
    cands: list[int] = []
    for i, line in enumerate(cart):
        if not any(gg.get("id") == group.get("id") for gg in _item_groups(menu_by_id, line)):
            continue
        cur = next((o for o in (line.get("selected_options") or []) if o.get("group_id") == group.get("id")), None)
        cur_name = ((cur or {}).get("option_name") or (cur or {}).get("name") or "").lower()
        if cur_name == target["name"].lower():
            continue
        if source is not None and cur_name != source["name"].lower():
            continue
        cands.append(i)
    if not cands:
        return None
    units = sum(cart[i]["quantity"] for i in cands)
    if units == 1 and source is None:
        # A single unit with an unnamed old value ("change to full cream") is
        # the established LLM replace_item path (replay conv_p015) -- leave it.
        return None
    if source is not None and cnt is None:
        cnt = units                       # "change the onion rings to potato skins"
    elif wants_all:
        cnt = units
    elif cnt is None:
        if units == 1:
            cnt = 1
        else:
            return EditPlan(
                "ask",
                question=f"How many of your {units} should I change to {target['name']}?",
            )
    if cnt > units:
        return EditPlan(
            "ask",
            question=f"You only have {units} that can be changed to {target['name']}. How many should I change?",
        )
    moves: list[tuple[int, int]] = []
    left = cnt
    for i in cands:
        take = min(left, cart[i]["quantity"])
        moves.append((i, take))
        left -= take
        if left == 0:
            break
    return EditPlan("option_change", moves=moves, group=group, target=target)


def apply_edit(session, plan: EditPlan, state_machine) -> list[str]:
    """Mutate the session cart per the plan; returns human labels."""
    from shared.pricing.engine import calculate_line_item
    cart = [dict(ln) for ln in state_machine.get_cart(session)]
    labels: list[str] = []
    if plan.kind == "set_quantity":
        ln = cart[plan.cart_index]
        bd = calculate_line_item(ln["base_price_cents"], ln.get("selected_options") or [],
                                 ln.get("add_ons") or [], plan.new_qty)
        ln["quantity"] = plan.new_qty
        ln["line_total_cents"] = bd.line_total_cents
        state_machine.set_cart(session, cart)
        return [f"{plan.new_qty}x {ln['name']}"]

    # option_change: split units off their lines, re-add with the new option
    adds: list[dict] = []
    for idx, units in plan.moves:
        ln = cart[idx]
        old_sel = ln.get("selected_options") or []
        old_opt = next((o for o in old_sel if o.get("group_id") == plan.group.get("id")), None)
        old_name = (old_opt or {}).get("option_name") or (old_opt or {}).get("name")
        new_sel = [o for o in old_sel if o.get("group_id") != plan.group.get("id")]
        new_sel.append({
            "group_id": plan.group.get("id"), "group_name": plan.group.get("name"),
            "option_id": plan.target.get("id"), "option_name": plan.target["name"],
            "name": plan.target["name"],
            "price_delta_cents": int(plan.target.get("price_delta_cents", 0) or 0),
        })
        # keep the group order the menu defines
        order = {g_id: n for n, g_id in enumerate([plan.group.get("id")])}
        parts = [p.strip() for p in str(ln.get("special_instructions") or "").split(",") if p.strip()]
        if old_name:
            parts = [plan.target["name"] if p.lower() == old_name.lower() else p for p in parts]
        if plan.target["name"] not in parts:
            parts.append(plan.target["name"])
        adds.append({
            "menu_item_id": ln["menu_item_id"], "name": ln["name"],
            "base_price_cents": ln["base_price_cents"], "quantity": units,
            "selected_options": new_sel, "add_ons": ln.get("add_ons") or [],
            "options": ln.get("options"),
            "special_instructions": ", ".join(parts) or None,
        })
        remaining = ln["quantity"] - units
        if remaining > 0:
            bd = calculate_line_item(ln["base_price_cents"], old_sel, ln.get("add_ons") or [], remaining)
            ln["quantity"] = remaining
            ln["line_total_cents"] = bd.line_total_cents
        else:
            ln["_remove"] = True
    state_machine.set_cart(session, [ln for ln in cart if not ln.get("_remove")])
    for a in adds:
        state_machine.add_to_cart(
            session, a["menu_item_id"], a["name"], a["base_price_cents"], a["quantity"],
            selected_options=a["selected_options"], add_ons=a["add_ons"],
            options=a["options"], special_instructions=a["special_instructions"],
        )
        labels.append(f"{a['quantity']}x {a['name']} → {plan.target['name']}")
    return labels
