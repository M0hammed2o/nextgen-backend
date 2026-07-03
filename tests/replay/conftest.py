"""
Pytest plugin — category-based summary reporter for the Conversation Replay Suite.

Hooks into pytest terminal output to display a per-category pass/fail table
at the end of each run, e.g.:

  ─────────────────────────────────────────────────────
  Conversation Replay Summary
  ─────────────────────────────────────────────────────
  greeting          PASS   5/  5
  menu              PASS   4/  4
  basic_ordering    PASS   6/  6
  modifiers         PASS   8/  8
  cart_edit         PASS   8/  8
  confirmation      PASS   4/  4
  pickup            PASS   3/  3
  delivery          PASS   3/  3
  recovery          PASS   3/  3
  sa_slang          PASS   9/  9
  long_conversation PASS   1/  1
  ─────────────────────────────────────────────────────
  TOTAL             PASS  54/ 54
  ─────────────────────────────────────────────────────
  Deployment: SAFE ✅
  ─────────────────────────────────────────────────────
"""

import re
from collections import defaultdict


# ── Category order for stable display ────────────────────────────────────────
_CATEGORY_ORDER = [
    "greeting",
    "menu",
    "basic_ordering",
    "modifiers",
    "cart_edit",
    "confirmation",
    "pickup",
    "delivery",
    "recovery",
    "sa_slang",
    "long_conversation",
    "proposed_items",
    "mixed_intent",
    "cancel_restart",
    "options",
    "pricing",
    "uncategorized",
]


class ReplayCategoryReporter:
    """Collects per-test results, grouped by category extracted from the test ID."""

    def __init__(self) -> None:
        # category → {"passed": int, "failed": int, "failed_ids": list[str]}
        self.results: dict[str, dict] = defaultdict(lambda: {"passed": 0, "failed": 0, "failed_ids": []})

    # ── Pytest hook ──────────────────────────────────────────────────────────

    def pytest_runtest_logreport(self, report) -> None:
        """Called after each test phase (setup / call / teardown)."""
        if report.when != "call":
            return
        if "test_conversation_replay" not in report.nodeid:
            return

        # Extract category from test ID: "conv_NNN [category] title"
        # The nodeid looks like:
        #   tests/replay/test_replay_suite.py::test_conversation_replay[conv_016 [greeting] title]
        # Phase 8: also handles conv_pNNN IDs (e.g. conv_p001 [pricing] ...)
        m = re.search(r"conv_\w+\s+\[([a-z_]+)\]", report.nodeid)
        category = m.group(1) if m else "uncategorized"

        entry = self.results[category]
        if report.passed:
            entry["passed"] += 1
        else:
            entry["failed"] += 1
            # Capture a short ID for the failure list
            short_id = report.nodeid.split("::")[-1][:60]
            entry["failed_ids"].append(short_id)

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config) -> None:
        """Print the category summary table after the standard summary."""
        if not self.results:
            return

        tw = terminalreporter._tw
        tw.sep("─", "Conversation Replay Summary")

        # Ordered categories first, then any extras
        ordered = [c for c in _CATEGORY_ORDER if c in self.results]
        extras = [c for c in sorted(self.results) if c not in ordered]

        total_pass = total_fail = 0
        col_w = max((len(c) for c in self.results), default=8) + 2

        for cat in ordered + extras:
            entry = self.results[cat]
            p, f = entry["passed"], entry["failed"]
            total_pass += p
            total_fail += f
            total = p + f
            status = "PASS" if f == 0 else "FAIL"
            colour = "green" if f == 0 else "red"
            tw.write(f"  {cat:<{col_w}}")
            tw.write(f"{status:6}", **{colour: True})
            tw.write(f"  {p:3}/{total:3}\n")
            for fid in entry["failed_ids"][:3]:
                tw.write(f"    ✗ {fid}\n", red=True)

        tw.sep("─", "")
        grand_total = total_pass + total_fail
        grand_status = "PASS" if total_fail == 0 else "FAIL"
        grand_colour = "green" if total_fail == 0 else "red"
        tw.write(f"  {'TOTAL':<{col_w}}")
        tw.write(f"{grand_status:6}", **{grand_colour: True})
        tw.write(f"  {total_pass:3}/{grand_total:3}\n")
        tw.sep("─", "")

        if total_fail == 0:
            tw.write("  Deployment: SAFE ✅\n", green=True)
        else:
            tw.write(f"  Deployment: BLOCKED — {total_fail} replay failure(s) ❌\n", red=True)

        tw.sep("─", "")


def pytest_configure(config) -> None:
    config.pluginmanager.register(ReplayCategoryReporter(), "replay_category_reporter")
