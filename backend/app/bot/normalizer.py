"""
SA WhatsApp message normalizer.

Applies targeted word-level corrections for South African WhatsApp ordering
patterns BEFORE intent routing and deterministic pattern matching.

Design principles:
  - Word-boundary protected: never alters characters inside a longer word
  - Conservative: only covers patterns that affect ordering intent
  - Original text is ALWAYS preserved by the caller (for LLM, DB, logging)
  - Fast: compiled regexes, single pass per rule
  - No semantic rewrites: "wanna" → "want" not "want to" (keeps structure)

What is NOT normalised here:
  - Digits ("2", "4") — too risky; quantity parser handles them directly
  - Generic pronouns ("r" → "are") — false-positive risk is too high
  - Emoji — preserved unchanged
  - Item names — the menu matcher handles casing/spacing independently
"""

import re as _re

# ── Rule registry ─────────────────────────────────────────────────────────────
# Each rule is (compiled_pattern, replacement_string).
# Rules are applied in the order they are registered.
# Multi-word patterns are registered before single-word patterns to prevent
# partial matches swallowing tokens the longer rule would consume.

_RULES: list[tuple[_re.Pattern, str]] = []


def _add(raw_pattern: str, replacement: str, *, flags: int = _re.I) -> None:
    """Register a rule with automatic word-boundary wrapping."""
    _RULES.append((_re.compile(rf"\b{raw_pattern}\b", flags), replacement))


def _add_raw(raw_pattern: str, replacement: str, *, flags: int = _re.I) -> None:
    """Register a rule WITHOUT auto word-boundaries (for multi-word or punctuation)."""
    _RULES.append((_re.compile(raw_pattern, flags), replacement))


# ── Punctuation shortcuts ─────────────────────────────────────────────────────
_add_raw(r"\bw/o\b", "without")        # w/o tomato  → without tomato
# w/ must be followed by whitespace (w/o catches w/o above first)
_add_raw(r"\bw/(?=\s)", "with ")       # "w/ extra"  → "with extra"

# ── Ingredient typos (most common in SA fast-food ordering) ──────────────────
_add(r"xtra",      "extra")            # xtra cheese → extra cheese
_add(r"cheez+",    "cheese")           # cheez/cheezz → cheese
_add(r"tamato",    "tomato")           # tamato      → tomato
_add(r"tamarto",   "tomato")           # tamarto     → tomato
_add(r"tamoto",    "tomato")           # tamoto      → tomato
_add(r"onyon",     "onion")            # onyon       → onion
_add(r"peper",     "pepper")           # peper       → pepper
_add(r"saace",     "sauce")            # saace       → sauce
_add(r"saus",      "sauce")            # saus        → sauce (Afrikaans-influenced)

# ── SA informal connectors / prepositions ─────────────────────────────────────
# "wit" = "with" in SA township slang; safe because word-boundary prevents
# matching "switch", "within", "whittle", etc.
_add(r"wit",       "with")             # wit no tomato → with no tomato
_add(r"wid",       "with")             # wid the burger → with the burger
_add(r"wout",      "without")          # wout tomato   → without tomato
_add(r"da",        "the")              # remove da burger → remove the burger

# ── Common abbreviations ──────────────────────────────────────────────────────
_add(r"cn",        "can")              # cn i get      → can i get
_add(r"cud",       "could")            # cud you       → could you
_add(r"dnt",       "don't")            # dnt put       → don't put
_add_raw(r"\bdont\b", "don't")         # dont put      → don't put (no apostrophe)
_add(r"pls",       "please")           # pls add       → please add
_add(r"plz",       "please")           # plz add       → please add
_add(r"gimme",     "give me")          # gimme a burger → give me a burger
_add(r"lemme",     "let me")           # lemme get     → let me get
_add(r"wanna",     "want")             # wanna add     → want add

# ── SA affirmative slang ──────────────────────────────────────────────────────
# These are normalised so that the is_confirmation patterns don't need
# duplicate entries for every spelling variant.
# NOTE: "sharp", "lekker", "yebo" are already in is_confirmation and left as-is.
_add(r"sho",       "sure")             # sho           → sure  (SA "sure/yes")
_add(r"aight",     "ok")               # aight         → ok
_add(r"aiight",    "ok")               # aiight        → ok
_add(r"kk",        "ok")               # kk            → ok  (WhatsApp shorthand)

# ── Informal "also" connector in ordering context ─────────────────────────────
# "also add" is redundant — strip "also" so "also add X" becomes "add X"
# which the modifier/ordering patterns can match cleanly.
# Placed last so earlier rules normalise any typos first.
_add_raw(r"\balso\s+(add|put|include)\b", r"\1")   # "also add" → "add"


# ── Public API ────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """
    Return a normalised copy of *text* suitable for deterministic pattern
    matching (intent routing, modifier detection, confirmation checks).

    The caller must preserve the original string for:
      - Database persistence  (already written before this is called)
      - LLM conversation history  (LLM handles natural language better raw)
      - Log lines that quote the customer's exact wording

    This function is idempotent: normalize(normalize(x)) == normalize(x).
    """
    if not text:
        return text

    result = text.strip()

    # Collapse runs of whitespace to a single space
    result = _re.sub(r"[ \t]{2,}", " ", result)

    # Apply all rules in registration order
    for pattern, replacement in _RULES:
        result = pattern.sub(replacement, result)

    return result
