"""
Mechanical repair for tests/replay fixtures made stale by commit 71afecb
("feat: add order-level special instructions", 2026-07-08), which added a
mandatory bot question ("Any special instructions for your order? ... Reply
*skip* if none.") in backend/app/bot/pipeline.py's _handle_order_confirmation,
right after all required customer details are collected and right before the
order is actually placed / parked for a delivery-fee approval. That commit
updated no replay fixtures, so every conversation that reaches order placement
or the delivery-fee-approval hand-off now stops one turn early, failing with:

    [turn N] state: expected 'ORDER_PLACED', got 'COLLECTING_DETAILS'
    [turn N] state: expected 'WAITING_DELIVERY_FEE_APPROVAL', got 'COLLECTING_DETAILS'

Repair semantics (verified against the live pipeline, see NOTE below)
-----------------------------------------------------------------------
For a conversation currently failing with one of the two messages above at
turn N:

  1. Turn N's own message is UNCHANGED (it is still the message that supplies
     the last missing detail / delivery address). Its `expect` block is
     REPLACED with the state the bot is actually in immediately after
     processing that message: COLLECTING_DETAILS, plus a response_contains
     check for the special-instructions prompt.
  2. A NEW turn `{"message": "skip"}` is inserted immediately AFTER turn N.
     Its `expect` block is turn N's *original* expect block, UNCHANGED.

NOTE on task-description wording: the task instructions describe this as
inserting the skip turn "immediately before the turn that expects
ORDER_PLACED / WAITING_DELIVERY_FEE_APPROVAL" and setting "the now-earlier
turn's" expectation to COLLECTING_DETAILS. Read as "the turn that keeps its
original message ends up earlier; the new skip turn ends up later, carrying
the moved expect block", this is exactly what this script does — verified
directly against the pipeline (see backend/app/bot/pipeline.py
_handle_order_confirmation ~3774-3783 and _handle_collecting_details
~4366-4374): the order-notes question is asked as a *result* of processing
the customer's last detail-collection message, so that same message's turn
must show COLLECTING_DETAILS; the ORDER_PLACED / WAITING_DELIVERY_FEE_APPROVAL
outcome can only be observed after a **subsequent** "skip" message. Inserting
the skip turn literally *before* turn N (keeping N's message as the one
answering "skip") would have the customer replying "skip" to a question that
has not been asked yet, and does not reproduce the real conversation. This
was confirmed empirically (see WORKLOG.md) before writing this script.

Conversations with two order cycles (e.g. conv_089) are repaired iteratively:
after the first insertion, the conversation is re-run; if a second occurrence
of the same failure pattern appears later in the conversation, a second
insertion is applied, and so on.

A conversation that fails for any OTHER reason (a different assertion, or the
same assertion at a turn with no "expect" block) is left completely untouched
and reported for manual diagnosis — this script never weakens or removes an
assertion.

Formatting
----------
Two fixture "families" exist in this repo:
  - Files that round-trip byte-for-byte (modulo CRLF/LF) through
    json.dumps(data, indent=2, ensure_ascii=True) — mostly the machine-
    generated tests/replay/conversations/synthetic/**, det_addon/**, and
    hardening/** fixtures. These are rewritten wholesale with json.dump;
    the diff is just the inserted/changed turn(s).
  - Hand-crafted fixtures directly under tests/replay/conversations/*.json,
    which use compact inline arrays/objects for readability and do NOT
    round-trip through json.dumps. These are repaired with a surgical
    text splice (bracket-depth-aware, string-literal-aware scanner) that
    only touches the exact character spans being changed, preserving all
    surrounding formatting (blank lines, inline arrays, key order) as-is.

Usage (run from the tests/replay/tools working dir's repo root, i.e.
nextgen-backend-v7/wt-replay):

    python -m tests.replay.tools.repair_notes_fixtures [--dry-run]

Idempotent: a conversation that already passes (including one this script
already repaired) is left untouched on a second run.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # .../wt-replay
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The bot pipeline logs at WARNING level on every branch decision — silence it
# so the repair report isn't drowned out. (No effect on assertion behaviour.)
logging.disable(logging.CRITICAL)

from tests.replay.fixtures import SA_MENU, make_business, make_customer  # noqa: E402
from tests.replay.framework import ReplayRunner  # noqa: E402

CONVERSATIONS_DIR = REPO_ROOT / "tests" / "replay" / "conversations"

TURN_STATE_RE = re.compile(
    r"\[turn (\d+)\] state: expected '(ORDER_PLACED|WAITING_DELIVERY_FEE_APPROVAL)', "
    r"got 'COLLECTING_DETAILS'"
)

NOTES_EXPECT = {
    "state": "COLLECTING_DETAILS",
    "response_contains": ["special instructions"],
}

MAX_CYCLES = 6  # generous ceiling; real conversations top out at 2 order cycles


# ── Simulation (dict-space) ─────────────────────────────────────────────────

async def _run_conv(conv: dict) -> None:
    business = make_business(conv.get("business", {}))
    customer = make_customer()
    runner = ReplayRunner(conv=conv, business=business, customer=customer, menu_items=SA_MENU)
    await runner.run()


def _try_run(conv: dict) -> tuple[bool, str | None]:
    try:
        asyncio.run(_run_conv(copy.deepcopy(conv)))
        return True, None
    except AssertionError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover — surfaced as "gave_up" for triage
        return False, f"{type(exc).__name__}: {exc}"


@dataclass
class RepairResult:
    outcome: str  # already_passing | repaired | unmatched_failure | gave_up
    marks: list[int] = field(default_factory=list)   # original turn indices touched
    working: dict | None = None                       # final passing dict (dict-space)
    detail: str | None = None


def _simulate_repair(conv: dict) -> RepairResult:
    """
    Determine (in dict-space, never touching disk) which ORIGINAL turn indices
    need the notes-question insertion, and confirm the fully-repaired
    conversation actually passes.
    """
    working = copy.deepcopy(conv)
    passed, msg = _try_run(working)
    if passed:
        return RepairResult(outcome="already_passing")

    marks: list[int] = []
    offset = 0
    for _ in range(MAX_CYCLES):
        m = TURN_STATE_RE.search(msg or "")
        if not m:
            return RepairResult(outcome="unmatched_failure", detail=msg)

        working_idx = int(m.group(1))
        original_idx = working_idx - offset
        turns = working["turns"]
        if not (0 <= working_idx < len(turns)):
            return RepairResult(outcome="unmatched_failure", detail=msg)

        turn = turns[working_idx]
        if "expect" not in turn:
            return RepairResult(outcome="unmatched_failure", detail=msg)

        moved_expect = turn["expect"]
        turn["expect"] = dict(NOTES_EXPECT)
        turns.insert(working_idx + 1, {"message": "skip", "expect": moved_expect})
        marks.append(original_idx)
        offset += 1

        passed, msg = _try_run(working)
        if passed:
            return RepairResult(outcome="repaired", marks=marks, working=working)

    return RepairResult(outcome="gave_up", detail=msg)


# ── Text-level bracket scanner (for surgical splicing) ──────────────────────

def _find_matching(text: str, open_idx: int) -> int:
    """Return the index of the bracket matching text[open_idx] ('{' or '[')."""
    depth = 0
    i = open_idx
    in_str = False
    escape = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    raise ValueError(f"no matching bracket for index {open_idx}")


def _turn_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) inclusive char offsets of each top-level turn object."""
    m = re.search(r'"turns"\s*:\s*\[', text)
    if not m:
        raise ValueError('no "turns" array found')
    array_start = m.end() - 1
    array_end = _find_matching(text, array_start)

    spans: list[tuple[int, int]] = []
    i = array_start + 1
    while i < array_end:
        c = text[i]
        if c == "{":
            end = _find_matching(text, i)
            spans.append((i, end))
            i = end + 1
        else:
            i += 1
    return spans, array_end


def _line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    return text[line_start:pos]


def _expect_value_span(text: str, turn_start: int, turn_end: int) -> tuple[int, int]:
    seg = text[turn_start:turn_end + 1]
    m = re.search(r'"expect"\s*:\s*', seg)
    if not m:
        raise ValueError("turn has no 'expect' key")
    val_start = turn_start + m.end()
    if text[val_start] not in "{[":
        raise ValueError("'expect' value is not an object/array")
    val_end = _find_matching(text, val_start)
    return val_start, val_end


def _splice_one_mark(text: str, spans: list[tuple[int, int]], span_idx: int, array_end: int) -> str:
    """
    Apply ONE insertion (skip turn after) + expect-rewrite (on the original
    turn) for the turn at spans[span_idx], using offsets from the ORIGINAL
    (pre-edit) text/spans. Safe to call repeatedly walking span_idx from
    high to low, always against `text` as progressively edited so far,
    because edits at a later original turn are always positioned entirely
    to the right of an earlier original turn's span.
    """
    turn_start, turn_end = spans[span_idx]
    next_start = spans[span_idx + 1][0] if span_idx + 1 < len(spans) else array_end

    expect_start, expect_end = _expect_value_span(text, turn_start, turn_end)
    moved_expect_text = text[expect_start:expect_end + 1]

    turn_indent = _line_indent(text, turn_start)
    # indent of the line the "expect" keyword itself sits on (key indent)
    expect_key_line_start = text.rfind("\n", 0, expect_start) + 1
    m = re.search(r'"expect"', text[expect_key_line_start:expect_start + 20])
    key_indent = text[expect_key_line_start:expect_key_line_start + (m.start() if m else 0)]
    if not key_indent.strip() == "" :
        key_indent = turn_indent + "  "  # fallback

    new_turn_text = (
        "{\n"
        f"{key_indent}\"message\": \"skip\",\n"
        f"{key_indent}\"expect\": {moved_expect_text}\n"
        f"{turn_indent}}}"
    )

    gap = text[turn_end + 1:next_start]
    if text[turn_end + 1] == ",":
        # not the last turn — reuse the existing comma+whitespace separator
        # both before and after the newly inserted turn.
        new_text = text[:turn_end + 1] + gap + new_turn_text + gap + text[next_start:]
    else:
        # this was the last turn in the array — synthesize a comma before the
        # new turn, then reuse the original tail (whitespace + "]") as-is.
        connector = "," + "\n" + turn_indent
        new_text = text[:turn_end + 1] + connector + new_turn_text + gap + text[next_start:]

    # Now rewrite the (unshifted, still-valid) expect span on the ORIGINAL
    # turn to the notes-question expectation.
    new_expect_text = (
        "{\n"
        f"{key_indent}  \"state\": \"COLLECTING_DETAILS\",\n"
        f"{key_indent}  \"response_contains\": [\"special instructions\"]\n"
        f"{key_indent}}}"
    )
    new_text = new_text[:expect_start] + new_expect_text + new_text[expect_end + 1:]
    return new_text


def _splice_all_marks(original_text: str, marks_original_idx: list[int]) -> str:
    spans, array_end = _turn_spans(original_text)
    text = original_text
    # Process from the LAST mark to the FIRST so earlier offsets stay valid.
    for original_idx in sorted(marks_original_idx, reverse=True):
        text = _splice_one_mark(text, spans, original_idx, array_end)
    return text


# ── Round-trip safety test ───────────────────────────────────────────────────

def _round_trip_safe(raw_text: str, data: dict) -> bool:
    regen = json.dumps(data, indent=2, ensure_ascii=True)
    normalized_original = raw_text.replace("\r\n", "\n").rstrip("\n")
    return normalized_original == regen


# ── Driver ───────────────────────────────────────────────────────────────────

def _iter_conversation_files() -> list[Path]:
    return sorted(CONVERSATIONS_DIR.glob("**/*.json"))


def main() -> int:
    # Windows consoles default to a legacy codepage that can't encode the
    # em-dashes / emoji that show up in bot response previews inside
    # assertion messages — reconfigure stdout to UTF-8 so the report never
    # crashes on print (this has no effect on file I/O, which is already
    # explicit UTF-8 throughout this script).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repaired: list[tuple[Path, int]] = []       # (path, num_insertions)
    already_passing = 0
    unmatched: list[tuple[Path, str]] = []
    gave_up: list[tuple[Path, str]] = []

    for path in _iter_conversation_files():
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)

        result = _simulate_repair(data)

        if result.outcome == "already_passing":
            already_passing += 1
            continue
        if result.outcome == "unmatched_failure":
            unmatched.append((path, result.detail or ""))
            continue
        if result.outcome == "gave_up":
            gave_up.append((path, result.detail or ""))
            continue

        # result.outcome == "repaired"
        assert result.working is not None
        if _round_trip_safe(raw_text, data):
            new_text = json.dumps(result.working, indent=2, ensure_ascii=True)
        else:
            new_text = _splice_all_marks(raw_text, result.marks)
            # Verify the splice produced valid JSON whose turns match the
            # dict-space simulation exactly before trusting it.
            spliced_data = json.loads(new_text)
            if spliced_data["turns"] != result.working["turns"]:
                gave_up.append((path, "surgical splice did not match dict-space simulation"))
                continue
            # Final live re-run against the ACTUAL spliced text (not just the
            # dict-space simulation) as a last confirmation before writing.
            passed, msg = _try_run(spliced_data)
            if not passed:
                gave_up.append((path, f"spliced text failed re-run: {msg}"))
                continue

        if not args.dry_run:
            path.write_text(new_text, encoding="utf-8", newline="\n")
        repaired.append((path, len(result.marks)))

    total_insertions = sum(n for _, n in repaired)
    print(f"Already passing (untouched): {already_passing}")
    print(f"Repaired:                    {len(repaired)} file(s), {total_insertions} turn insertion(s)")
    print(f"Unmatched failure (skipped): {len(unmatched)}")
    for p, d in unmatched:
        print(f"  - {p.relative_to(REPO_ROOT)}: {d[:160]}")
    print(f"Gave up (skipped):           {len(gave_up)}")
    for p, d in gave_up:
        print(f"  - {p.relative_to(REPO_ROOT)}: {d[:160]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
