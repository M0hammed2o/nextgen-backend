"""
Intent Router — rules-first, LLM-second.

Keyword matching handles ~60-70% of messages without any LLM call.
Only ambiguous/free-form messages go to the LLM.
This is how you hit R1000+ profit per business.
"""

import re

from shared.enums import MessageIntent


# ── Keyword patterns (compiled once, reused forever) ─────────────────────────

_PATTERNS: list[tuple[re.Pattern, MessageIntent]] = [
    # Opt-out (highest priority — Meta compliance)
    (re.compile(r"\b(stop|unsubscribe|opt.?out|cancel.?sub)\b", re.I), MessageIntent.OPT_OUT),

    # Greetings
    (re.compile(
        r"^(hi|hello|hey|howzit|heita|yebo|yo|sup|good\s*(morning|afternoon|evening)|"
        r"sawubona|molo|hola|ola|gday)\b", re.I
    ), MessageIntent.GREETING),

    # Recommendations — intercept BEFORE menu so "what do you recommend" never
    # hits the menu dump. These route to the LLM for a natural response.
    (re.compile(
        r"\b(recommend|recommendation|popular|best.?sell|best.?seller|"
        r"what.?s good|what is good|what.?s nice|what.?s great|"
        r"what.?s your best|what do you suggest|suggest|"
        r"most ordered|fan.?fav|fan favourite|fan favorite|must.?try|"
        r"what should i (get|order|try|have)|worth trying)\b", re.I
    ), MessageIntent.RECOMMENDATION),

    # Menu requests — recommendation phrases handled above
    (re.compile(
        r"\b(menu|food|eat|"
        r"what\s+(do\s+you\s+(have|sell|serve)|.*(have|sell|serve))|"
        r"what.*available|price list|show me|items|catalog|catalogue)\b", re.I
    ), MessageIntent.MENU_REQUEST),

    # Specials
    (re.compile(
        r"\b(special|specials|deal|deals|promo|promotion|discount|"
        r"today.?s.*special|weekly.*special)\b", re.I
    ), MessageIntent.SPECIALS_REQUEST),

    # Hours
    (re.compile(
        r"\b(hours|open|close|closing|opening|when.*open|what time|"
        r"are you open|still open|operating)\b", re.I
    ), MessageIntent.HOURS_REQUEST),

    # Location
    (re.compile(
        r"\b(location|where|address|directions|find you|"
        r"where are you|how.*get.*there)\b", re.I
    ), MessageIntent.LOCATION_REQUEST),

    # Order tracking
    (re.compile(
        r"\b(track|status|where.*my.*order|order.*status|how long|"
        r"ready.*yet|is it ready|when.*ready|eta)\b", re.I
    ), MessageIntent.ORDER_TRACK),

    # Remove item from cart — must come BEFORE ORDER_START so that phrases like
    # "Take out from my order the Classic Smash Burger" (which contains "order")
    # are correctly classified as ORDER_REMOVE instead of ORDER_START.
    # Covers SA natural-language phrasing.
    (re.compile(
        r"\b(remove|take\s+off|take\s+out|take\s+away|delete|"
        r"leave\s+out|leave\s+off|drop|no\s+more|don.?t\s+want\s+the)\b", re.I
    ), MessageIntent.ORDER_REMOVE),

    # Order cancel — must come BEFORE ORDER_START so that "cancel order",
    # "start over", "clear my order" are not swallowed by the \border\b pattern.
    (re.compile(
        r"\b(cancel|nevermind|never mind|forget it|don.?t want|"
        r"start over|clear|remove all|scratch that)\b", re.I
    ), MessageIntent.ORDER_CANCEL),

    # Order start (explicit)
    (re.compile(
        r"\b(order|i.?d like|i want|can i get|give me|"
        r"let me get|i.?ll have|place.*order)\b", re.I
    ), MessageIntent.ORDER_START),

    # Order confirm (in context of cart)
    (re.compile(
        r"^(yes|yep|yeah|yah|sure|confirm|that.?s (it|all|correct|right)|"
        r"looks good|perfect|sharp|100|lekker|right|cool|ok|okay|done|"
        r"place it|send it|go ahead)\s*[.!]*$", re.I
    ), MessageIntent.ORDER_CONFIRM),

    # Add more to cart
    # NOTE: "also", "and", "with" are intentionally excluded here — they are too
    # common and cause false positives on questions ("Also how is ur wings?").
    # Those words only signal ordering intent when combined with an explicit
    # item reference, which the LLM handles naturally via the None → LLM path.
    (re.compile(
        r"\b(add|plus|another|extra)\b", re.I
    ), MessageIntent.ORDER_ADD),

    # View cart
    (re.compile(
        r"\b(cart|my order|what.*(did i|have i).*(order|add)|show.*order|view.*cart|"
        r"current.*order|order.*so far|what.*(in|on).*my.*cart)\b", re.I
    ), MessageIntent.VIEW_CART),

    # Human handoff (high priority — customer wants a real person)
    (re.compile(
        r"\b(human|staff|agent|person|someone|call me|speak to|talk to|"
        r"real person|manager|help.*person|connect me)\b", re.I
    ), MessageIntent.HUMAN_HANDOFF),
]


def match_intent(text: str) -> MessageIntent | None:
    """
    Try to match the message text against keyword rules.
    Returns the first matching intent, or None if no rule matches.
    
    None means "send to LLM for classification".
    """
    text = text.strip()
    if not text:
        return None

    for pattern, intent in _PATTERNS:
        if pattern.search(text):
            return intent

    return None


def needs_llm(intent: MessageIntent | None, conversation_state: str) -> bool:
    """
    Decide whether this message needs an LLM call.
    
    Rules:
    - No intent matched → LLM needed
    - ORDER_START/ORDER_ADD with ambiguous item reference → LLM needed
    - BUILDING_CART state + free-form text → LLM needed to parse items
    - Everything else → handle with templates
    """
    if intent is None:
        return True

    # These always need LLM to understand what the customer wants to order
    if intent in (MessageIntent.ORDER_START, MessageIntent.ORDER_ADD, MessageIntent.ORDER_REMOVE):
        return True  # LLM parses the specific items/quantities/removals

    # Recommendations go to LLM for a natural conversational response
    if intent == MessageIntent.RECOMMENDATION:
        return True

    # If in cart-building state, most messages need LLM for item parsing
    if conversation_state in ("BUILDING_CART", "CHOOSING_OPTIONS"):
        if intent == MessageIntent.UNKNOWN:
            return True

    return False


def is_confirmation(text: str) -> bool:
    """
    Check if a message is a clear yes/confirmation.

    Covers:
      - Single-word affirmatives: yes, yep, yeah, sharp, lekker, sho, ya, correct …
      - Sentence-form affirmatives: "that is correct", "this is right", "place the order" …
      - SA-specific: "no this is correct" (= "no [changes], this IS correct"), "sho", "ya"

    Allows polite trailing words: "yes please", "yes thanks bru".
    Does NOT match messages that add or change content: "yes but change the chips".
    """
    stripped = text.strip()

    # ── Special case: "No, this is correct/right/fine" ───────────────────────
    # SA customers often use "No" to mean "No [changes needed], this is correct."
    # Detect "No[,.]? this/that is <affirmative>" before the general pattern so it
    # is not swallowed by is_negation (which requires "no" at end-of-string).
    _NO_THIS_IS = re.compile(
        r"^no[,.]?\s+(?:this|that)\s+is\s+"
        r"(?:correct|right|fine|good|perfect|the\s+(?:correct|right)\s+order|"
        r"what\s+i\s+want|my\s+order)\s*[.!]*$",
        re.I,
    )
    if _NO_THIS_IS.match(stripped):
        return True

    # ── General affirmative pattern ───────────────────────────────────────────
    _CORE = (
        # Single-word / short-phrase affirmatives
        r"yes|yep|yeah|yah|ya|yebo|ja|jah|"
        r"sure\s+thing|sure|sharp|sho|"
        r"confirm|confirmed|correct|"
        r"looks\s+good|perfect|lekker|"
        r"100|right|cool|ok|okay|done|"
        r"place\s+it|send\s+it|"
        r"go\s+ahead|let.?s\s+go|do\s+it|sounds\s+good|all\s+good|proceed|carry\s+on|"
        # Sentence-form affirmatives: "that's correct / right / fine / good"
        r"that.?s\s+(it|all|correct|right|fine|good|my\s+order|the\s+order|what\s+i\s+want)|"
        r"that\s+is\s+(correct|right|fine|good|it|my\s+order)|"
        # "this is correct / right / fine"
        r"this\s+is\s+(correct|right|fine|good|my\s+order|the\s+(?:correct|right)\s+order)|"
        # "place [the] order" / "confirm [the] order"
        r"place\s+(?:the\s+)?order|confirm\s+(?:the\s+)?order|"
        # "go ahead [with it/the order]"
        r"go\s+ahead\s+(?:with\s+(?:it|that|the\s+order))?"
    )
    _TRAILING = (
        r"(\s+("
        r"please|thanks|thank\s+you|bru|man|mate|now|"
        r"sure\s+thing|great|awesome|for\s+sure|"
        r"confirm|confirmed|proceed|place|order|my\s+order|the\s+order|it|"
        # standalone affirmative words valid as trailing qualifiers
        r"correct|right|fine|good|"
        # compound trailing: "that's correct / right / fine" (e.g. "sharp that's correct")
        r"that.?s\s+(?:correct|right|fine|good|it)|"
        r"delivery|for\s+delivery|pickup|for\s+pickup|collection|for\s+collection"
        r"))*"
    )
    confirmations = re.compile(rf"^({_CORE}){_TRAILING}\s*[,!.]*$", re.I)
    return bool(confirmations.match(stripped))


def is_negation(text: str) -> bool:
    """Check if a message is a clear no/negation."""
    negations = re.compile(
        r"^(no|nah|nope|not\s+yet|not\s+now|wait|hold\s+on|actually|"
        r"cancel\s+that|scratch\s+that|never\s+mind|nevermind)\s*[,!.]*$", re.I
    )
    return bool(negations.match(text.strip()))


def is_recommendation_acceptance(text: str) -> bool:
    """
    Check if a message is accepting a prior recommendation from the bot.
    Only matches messages that are clearly about the recommendation, not
    generic confirmations (those are handled by is_confirmation in CONFIRMING_ORDER).

    Examples that match:
      "I'll take what you recommend"    "ok I'll have that"
      "take the recommendation"         "I'll take those"
      "give me what you recommended"    "I'll go with that"
      "sounds good I'll take those"     "that works for me"
    """
    pattern = re.compile(
        r"^("
        # explicit "take/have/get what you recommend/suggest/said"
        r"i.?ll?\s+(take|have|get|go\s+with)\s+(what\s+(you|u)\s+(recommend\w*|suggest\w*|said)|"
        r"those?|that|your\s+recommendation\w*|the\s+recommendation\w*)"
        r"|take\s+(the\s+)?(recommendation\w*|those?|that|what\s+(you|u)\s+(recommend\w*|suggest\w*))"
        r"|give\s+me\s+(what\s+(you|u)\s+(recommend\w*|suggest\w*)|those?|that)"
        r"|i.?ll?\s+have\s+(what\s+(you|u).{0,15}(recommend\w*|suggest\w*)|those?|that)"
        r"|i.?ll?\s+go\s+with\s+(those?|that|your\s+\w+|the\s+\w+)"
        r"|go\s+with\s+(those?|that|your\s+recommendation\w*|the\s+recommendation\w*)"
        # short acceptance with explicit reference to "those"
        r"|(ok|okay|sure|yes|yeah|yep)\s*[,.]?\s*i.?ll?\s+(take|have|get)\s+(those?|that|them)"
        r"|(ok|okay|sure)\s*[,.]?\s*(give\s+me\s+(those?|that)|i.?ll?\s+go\s+with\s+(those?|that))"
        # "add what you recommend" / "yes please add what u recommend" / "add those recommendations"
        r"|(yes\s+please\s+|please\s+)?(add|give\s+me)\s+(what\s+(you|u)\s+(recommend\w*|suggest\w*)|those?\s+recommendation\w*|the\s+recommendation\w*)"
        r"|(yes|yeah|ok|okay|sure)\s*[,.]?\s*(please\s+)?(add|give\s+me)\s+(those?|that|them|all|what\s+(you|u)\s+(recommend\w*|suggest\w*))"
        r")\s*[.!]*$",
        re.I
    )
    return bool(pattern.match(text.strip()))


def is_cart_correction(text: str) -> bool:
    """
    Check if a message in CONFIRMING_ORDER is the customer restating their
    full desired order (correcting quantities), not adding new items on top.

    These should clear the cart and rebuild from scratch:
      "No one of each item I want"
      "I only want one of each"
      "No, just one of each"
      "Actually I want only one of each"
      "Make it just one of each"

    These should NOT match (simple add-ons or removals):
      "Add an ice coffee"
      "Remove the tomato"
      "I want to add wings"
    """
    pattern = re.compile(
        r"("
        # "only want one/a of each/every"
        r"(i\s+)?(only\s+want|just\s+want|want\s+only)\s+(one|1|a\s+single)\s+of\s+(each|every)"
        # "one of each" as a standalone correction
        r"|(no[,.]?\s+)?(just\s+)?(one|1)\s+of\s+(each|every)"
        # "no one of each" / "no, one of each"
        r"|(no[,.]?\s+)(one|1)\s+of\s+(each|every)"
        # "actually/no I want only" / "make it just"
        r"|(actually|no)[,!]?\s+(i\s+)?(only\s+want|want\s+only|just\s+want)"
        r"|make\s+it\s+just"
        # "Make it only..." — e.g. "Make it only one burger" / "Actually make it only one burger"
        r"|make\s+it\s+only"
        # "No it must be..." — customer restating what they actually want
        r"|no[,.]?\s+it\s+must\s+be"
        # "Actually I only want..." is already covered by the (actually|no) branch above;
        # this covers "Actually make it..." without a following want/just/only phrase
        r"|actually\s+make\s+it"
        r")",
        re.I,
    )
    return bool(pattern.search(text.strip()))
