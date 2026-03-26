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

    # Menu requests
    (re.compile(
        r"\b(menu|food|eat|what.*(do you|have|sell|serve)|price list|"
        r"what.*available|show me|items|catalog|catalogue)\b", re.I
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

    # Order cancel
    (re.compile(
        r"\b(cancel|nevermind|never mind|forget it|don.?t want|"
        r"start over|clear|remove all|scratch that)\b", re.I
    ), MessageIntent.ORDER_CANCEL),

    # Remove item from cart
    (re.compile(
        r"\b(remove|take off|delete|no more|don.?t want the)\b", re.I
    ), MessageIntent.ORDER_REMOVE),

    # Add more to cart
    (re.compile(
        r"\b(add|also|and|plus|another|more|extra|with)\b", re.I
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
    if intent in (MessageIntent.ORDER_START, MessageIntent.ORDER_ADD):
        return True  # LLM parses the specific items/quantities

    # If in cart-building state, most messages need LLM for item parsing
    if conversation_state in ("BUILDING_CART", "CHOOSING_OPTIONS"):
        if intent == MessageIntent.UNKNOWN:
            return True

    return False


def is_confirmation(text: str) -> bool:
    """Check if a message is a clear yes/confirmation."""
    confirmations = re.compile(
        r"^(yes|yep|yeah|yah|sure|confirm|that.?s (it|all|correct|right)|"
        r"looks good|perfect|sharp|100|lekker|right|cool|ok|okay|done|"
        r"place it|send it|go ahead)\s*[.!]*$", re.I
    )
    return bool(confirmations.match(text.strip()))


def is_negation(text: str) -> bool:
    """Check if a message is a clear no/negation."""
    negations = re.compile(
        r"^(no|nah|nope|not yet|wait|hold on|actually)\s*[.!]*$", re.I
    )
    return bool(negations.match(text.strip()))
