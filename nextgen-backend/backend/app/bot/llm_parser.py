"""
LLM Response Parser — extracts structured action data from LLM output.

The LLM is instructed to end responses with a JSON block.
This parser extracts that block and validates it.
"""

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("nextgen.bot.parser")


@dataclass
class ParsedItem:
    name: str | None
    quantity: int = 1
    options: dict = field(default_factory=dict)
    special_instructions: str | None = None
    original_text: str | None = None  # For unmatched items


@dataclass
class ParsedLLMResponse:
    action: str  # add_items, remove_item, confirm_order, cancel_order, ask_options, chitchat, handoff
    items: list[ParsedItem]
    message: str
    raw_json: dict | None = None


def parse_llm_response(raw_text: str) -> ParsedLLMResponse:
    """
    Parse the LLM response, extracting the JSON action block.
    Falls back gracefully if parsing fails.
    """
    # Try to extract JSON block
    json_data = _extract_json(raw_text)

    if json_data:
        try:
            action = json_data.get("action", "chitchat")
            message = json_data.get("message", "")
            items_raw = json_data.get("items", [])

            items = []
            for item_data in items_raw:
                if isinstance(item_data, dict):
                    items.append(ParsedItem(
                        name=item_data.get("name"),
                        quantity=item_data.get("quantity", 1),
                        options=item_data.get("options", {}),
                        special_instructions=item_data.get("special_instructions"),
                        original_text=item_data.get("original_text"),
                    ))

            # If message is empty, use the text before the JSON block
            if not message:
                message = _extract_text_before_json(raw_text)

            return ParsedLLMResponse(
                action=action,
                items=items,
                message=message,
                raw_json=json_data,
            )
        except Exception as e:
            logger.warning("Failed to parse LLM JSON structure: %s", e)

    # Fallback: treat entire response as a chitchat message
    clean_text = _clean_response_text(raw_text)
    return ParsedLLMResponse(
        action="chitchat",
        items=[],
        message=clean_text,
    )


def parse_items_response(raw_text: str) -> list[ParsedItem]:
    """
    Parse a response from the item-parsing prompt.
    Expects a JSON array of items.
    """
    json_data = _extract_json_array(raw_text)
    if not json_data:
        return []

    items = []
    for item_data in json_data:
        if isinstance(item_data, dict):
            items.append(ParsedItem(
                name=item_data.get("name"),
                quantity=item_data.get("quantity", 1),
                options=item_data.get("options", {}),
                special_instructions=item_data.get("special_instructions"),
                original_text=item_data.get("original_text"),
            ))
    return items


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from text (handles markdown code blocks)."""
    # Try to find ```json ... ``` block
    pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
    match = re.search(pattern, text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find a raw JSON object at the end
    # Look for the last { ... } block
    brace_depth = 0
    start = None
    for i in range(len(text) - 1, -1, -1):
        if text[i] == '}':
            if brace_depth == 0:
                end = i
            brace_depth += 1
        elif text[i] == '{':
            brace_depth -= 1
            if brace_depth == 0:
                start = i
                break

    if start is not None:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _extract_json_array(text: str) -> list | None:
    """Extract a JSON array from text."""
    # Try ```json ... ``` block
    pattern = r"```(?:json)?\s*(\[[\s\S]*?\])\s*```"
    match = re.search(pattern, text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try raw array
    pattern = r"\[[\s\S]*\]"
    match = re.search(pattern, text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _extract_text_before_json(text: str) -> str:
    """Get the human-readable text before the JSON block."""
    # Remove JSON block and code fences
    cleaned = re.sub(r"```(?:json)?[\s\S]*?```", "", text)
    cleaned = re.sub(r"\{[\s\S]*\}\s*$", "", cleaned)
    return cleaned.strip()


def _clean_response_text(text: str) -> str:
    """Clean LLM response text for WhatsApp display."""
    # Remove JSON blocks
    text = re.sub(r"```(?:json)?[\s\S]*?```", "", text)
    # Remove trailing JSON objects
    text = re.sub(r"\{[\s\S]*\}\s*$", "", text)
    return text.strip()
