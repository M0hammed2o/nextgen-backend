"""
Per-unit option allocation for multi-quantity cart lines.

Background (live incident, 2026-11-11, Super Burger): "3 Super Burgers" with a
required Patty group and a required Side group could only ever carry ONE value
per group for ALL units ("Beef, Fries" on 3x). A customer saying "2 with fries
and 1 with onion rings" had no representation, and the side words were
interpreted as brand-new standalone products (a separate R32 "Onion Rings").

This module is pure (no DB, no I/O) so the pipeline's deterministic layer can
resolve option answers without an LLM:

  * ``parse_allocation``  -- read "2 with fries and one with onion rings" /
    "fries for 2" / "all beef" / a bare "Beef" for N awaiting units into
    (option, count) picks. Never invents a choice: ambiguous or over-allocated
    answers are reported, not guessed.
  * ``apply_text_to_pending`` -- apply a customer message to a pending item
    (``pending_options`` entry), group by group, to the units still awaiting
    that group.
  * ``build_lines`` -- expand per-group allocations into cart lines (each a
    distinct option combination with its quantity), which is the cart's
    existing per-unit representation (add_to_cart merges identical lines).

Pending item shape (additions to the existing pending_options dict):
    "allocations": {group_id: [{"option": {id, name, price_delta_cents,
                                           group_id, group_name}, "count": n}]}
A group is complete when its counts sum to the pending ``quantity``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_ALL_WORDS = {"all", "both", "each", "every", "everyone", "everything"}
_NUM_RE = re.compile(
    r"(?<![a-z0-9])(?:(\d{1,2})\s*x?|x\s*(\d{1,2})|(" + "|".join(_WORD_NUMS) + r")|("
    + "|".join(_ALL_WORDS) + r"))(?![a-z0-9])",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r"[,;.\n]|\band\b|\balso\b|\bplus\b|\bthen\b|\bbut\b|&", re.I)
# Words that mark a mention as an explicit EXTRA / separate item request.
EXTRA_MARKER_RE = re.compile(
    r"\b(extra|additional|another|separate|separately|on\s+top|as\s+well|in\s+addition|"
    r"on\s+the\s+side\s+as\s+well|more)\b",
    re.I,
)


@dataclass
class Mention:
    option: dict
    start: int
    end: int


@dataclass
class AllocationResult:
    # "none"    -> no option of this group named in the text
    # "ok"      -> counts sum to qty
    # "partial" -> counts sum to less than qty (rest still awaiting)
    # "over"    -> counts exceed qty (ambiguous; nothing applied)
    # "unclear" -> several options named with no counts for >1 unit
    status: str
    picks: list[tuple[dict, int]] = field(default_factory=list)


def _forms(name: str) -> list[str]:
    n = name.lower().strip()
    forms = {n}
    if n.endswith("ies"):
        forms.add(n[:-3] + "y")
    elif n.endswith("s"):
        forms.add(n[:-1])
    else:
        forms.add(n + "s")
    return sorted(forms, key=len, reverse=True)


def enabled_options(group: dict) -> list[dict]:
    return [o for o in group.get("options", []) if o.get("is_enabled", True)]


def required_groups(options_json: dict | None) -> list[dict]:
    groups = (options_json or {}).get("option_groups", []) or []
    return [g for g in groups if g.get("required") and g.get("is_enabled", True)]


def find_mentions(options: list[dict], text: str) -> list[Mention]:
    """Option names (and simple plural/singular forms) named in ``text``,
    word-bounded, longest names first, never overlapping."""
    low = text.lower()
    taken: list[tuple[int, int]] = []
    found: list[Mention] = []
    for opt in sorted(options, key=lambda o: len(o["name"]), reverse=True):
        for form in _forms(opt["name"]):
            for m in re.finditer(rf"(?<![a-z0-9]){re.escape(form)}(?![a-z0-9])", low):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                found.append(Mention(opt, m.start(), m.end()))
    found.sort(key=lambda x: x.start)
    return found


def _numbers_in(text: str, qty: int, blocked_following: set[str]) -> list[tuple[int, int, int]]:
    """(count, start, end) for number tokens in ``text``. A number directly
    followed by an item word ("3 super burgers") is the item quantity, not an
    option count, and is skipped."""
    out: list[tuple[int, int, int]] = []
    for m in _NUM_RE.finditer(text):
        if m.group(1) or m.group(2):
            n = int(m.group(1) or m.group(2))
        elif m.group(3):
            n = _WORD_NUMS[m.group(3).lower()]
        else:
            n = qty  # all / both / each
        follow = re.match(r"\s*x?\s*([a-z']+)", text[m.end():], re.I)
        if follow and follow.group(1).lower().rstrip("s") in blocked_following and not m.group(4):
            continue
        out.append((n, m.start(), m.end()))
    return out


def parse_allocation(
    text: str,
    options: list[dict],
    qty: int,
    item_words: set[str] | None = None,
) -> AllocationResult:
    """Read an option answer for ``qty`` units awaiting one option each."""
    blocked = {w.lower().rstrip("s") for w in (item_words or set())}
    mentions = [
        m for m in find_mentions(options, text)
        # "... and an extra onion rings": a clearly requested EXTRA is its own
        # product, not this unit's included option.
        if not EXTRA_MARKER_RE.search(text.lower()[max(0, m.start - 25):m.start])
    ]
    if not mentions:
        return AllocationResult("none")

    low = text.lower()
    # clause boundaries
    bounds = [0] + [m.end() for m in _CLAUSE_SPLIT_RE.finditer(low)] + [len(low)]
    spans = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    def clause_of(pos: int) -> tuple[int, int]:
        for s, e in spans:
            if s <= pos < e or (pos == e == len(low)):
                return s, e
        return 0, len(low)

    counts: dict[int, int | None] = {}
    used_numbers: set[tuple[int, int]] = set()
    for idx, men in enumerate(mentions):
        cs, ce = clause_of(men.start)
        seg = low[cs:ce]
        nums = [
            (n, s + cs, e + cs) for n, s, e in _numbers_in(seg, qty, blocked)
            if (s + cs, e + cs) not in used_numbers
        ]
        best = None
        best_d = None
        for n, s, e in nums:
            d = (men.start - e) if e <= men.start else (s - men.end)
            d = d if d >= 0 else 10_000
            # prefer a number BEFORE the mention on ties ("2 with fries")
            key = (d, 0 if e <= men.start else 1)
            if best is None or key < best_d:
                best, best_d = (n, s, e), key
        if best is not None and best_d[0] <= 40:
            counts[idx] = best[0]
            used_numbers.add((best[1], best[2]))
        else:
            counts[idx] = None

    # merge repeated mentions of the same option (sum their counts)
    known = sum(c for c in counts.values() if c is not None)
    unknown = [i for i, c in counts.items() if c is None]

    picks: dict[str, tuple[dict, int]] = {}

    def add(opt: dict, n: int) -> None:
        key = str(opt.get("id") or opt["name"])
        cur = picks.get(key)
        picks[key] = (opt, (cur[1] if cur else 0) + n)

    distinct_unknown = {str(mentions[i].option.get("id") or mentions[i].option["name"]) for i in unknown}
    if not unknown:
        for i, c in counts.items():
            add(mentions[i].option, c)
    elif len(distinct_unknown) == 1 and len(unknown) >= 1:
        # exactly one option without a count takes the remainder (all units if
        # it is the only thing named): "2 with fries and the rest onion rings"
        # / bare "Beef".
        rest = qty - known
        if rest <= 0:
            return AllocationResult("over")
        for i, c in counts.items():
            if c is not None:
                add(mentions[i].option, c)
        add(mentions[unknown[0]].option, rest)
    else:
        if qty == 1:
            return AllocationResult("unclear")
        return AllocationResult("unclear")

    total = sum(n for _, n in picks.values())
    plist = [p for p in picks.values() if p[1] > 0]
    if total == qty:
        return AllocationResult("ok", plist)
    if total < qty:
        return AllocationResult("partial", plist)
    return AllocationResult("over", plist)


# ── Pending-item helpers ─────────────────────────────────────────────────────

def _opt_entry(group: dict, opt: dict) -> dict:
    return {
        "id": opt.get("id"),
        "name": opt["name"],
        "price_delta_cents": int(opt.get("price_delta_cents", 0) or 0),
        "group_id": group.get("id"),
        "group_name": group.get("name"),
    }


def group_assigned(pending: dict, group: dict) -> int:
    return sum(a["count"] for a in (pending.get("allocations") or {}).get(group.get("id"), []))


def group_remaining(pending: dict, group: dict) -> int:
    return max(0, int(pending.get("quantity") or 1) - group_assigned(pending, group))


def missing_groups(pending: dict) -> list[dict]:
    return [g for g in required_groups(pending.get("options_json")) if group_remaining(pending, g) > 0]


def seed_from_legacy(pending: dict) -> None:
    """Pending items written by older code carry ``selected_options`` (one
    answer for all units). Fold those into allocations once."""
    if pending.get("allocations") is not None:
        return
    alloc: dict = {}
    qty = int(pending.get("quantity") or 1)
    groups = {g.get("id"): g for g in required_groups(pending.get("options_json"))}
    for so in pending.get("selected_options") or []:
        gid = so.get("group_id")
        if gid in groups:
            alloc[gid] = [{
                "option": {
                    "id": so.get("option_id"), "name": so.get("option_name") or so.get("name"),
                    "price_delta_cents": so.get("price_delta_cents", 0),
                    "group_id": gid, "group_name": so.get("group_name"),
                },
                "count": qty,
            }]
    pending["allocations"] = alloc


def apply_text_to_pending(
    pending: dict, text: str, item_words: set[str] | None = None,
) -> tuple[bool, list[tuple[dict, str]]]:
    """Apply a customer message to a pending item, group by group, to the
    units still awaiting each group. Returns (changed, issues) where issues
    are (group, status) for groups the text addressed but could not be
    applied (over-allocated / unclear). Mutates ``pending``."""
    seed_from_legacy(pending)
    changed = False
    issues: list[tuple[dict, str]] = []
    for group in required_groups(pending.get("options_json")):
        remaining = group_remaining(pending, group)
        if remaining <= 0:
            continue
        res = parse_allocation(text, enabled_options(group), remaining, item_words)
        if res.status in ("ok", "partial"):
            bucket = pending["allocations"].setdefault(group.get("id"), [])
            for opt, n in res.picks:
                for existing in bucket:
                    if existing["option"].get("id") == opt.get("id") and existing["option"]["name"] == opt["name"]:
                        existing["count"] += n
                        break
                else:
                    bucket.append({"option": _opt_entry(group, opt), "count": n})
            changed = True
        elif res.status in ("over", "unclear"):
            issues.append((group, res.status))
    return changed, issues


def reset_for_quantity(pending: dict, new_qty: int) -> None:
    """Quantity changed -> earlier per-unit allocations no longer line up.
    Scale a uniform (single-option-per-group) answer; drop split answers."""
    old = int(pending.get("quantity") or 1)
    seed_from_legacy(pending)
    for gid, entries in list((pending.get("allocations") or {}).items()):
        if len(entries) == 1:
            entries[0]["count"] = new_qty
        else:
            del pending["allocations"][gid]
    pending["quantity"] = new_qty
    pending.pop("selected_options", None)
    if old == new_qty:
        return


def build_lines(pending: dict) -> list[dict]:
    """Expand allocations into cart lines: [{quantity, selected_options,
    option_names}] -- one per distinct option combination, in first-seen order.
    Units are paired across groups in allocation order."""
    qty = int(pending.get("quantity") or 1)
    groups = required_groups(pending.get("options_json"))
    expanded: dict = {}
    for g in groups:
        seq: list[dict] = []
        for a in (pending.get("allocations") or {}).get(g.get("id"), []):
            seq.extend([a["option"]] * a["count"])
        expanded[g.get("id")] = seq[:qty]
    lines: list[dict] = []
    index: dict = {}
    for u in range(qty):
        sel = []
        for g in groups:
            seq = expanded.get(g.get("id")) or []
            if u < len(seq):
                o = seq[u]
                sel.append({
                    "group_id": o.get("group_id"), "group_name": o.get("group_name"),
                    "option_id": o.get("id"), "option_name": o["name"], "name": o["name"],
                    "price_delta_cents": o.get("price_delta_cents", 0),
                })
        key = tuple((s["group_id"], s["option_id"], s["option_name"]) for s in sel)
        if key in index:
            lines[index[key]]["quantity"] += 1
        else:
            index[key] = len(lines)
            lines.append({"quantity": 1, "selected_options": sel, "option_names": [s["option_name"] for s in sel]})
    return lines
