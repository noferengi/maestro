"""
app/agent/json_utils.py
-----------------------
Shared JSON extraction utilities for agent output parsing.

LLM responses frequently embed JSON inside fenced code blocks or as bare
objects mixed with prose.  The helpers here provide a single, well-tested
extraction path used by all agents.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json_block(text: str) -> str | None:
    """Extract the first JSON object from agent output.

    Tries, in order:
      1. A fenced code block: ```json { ... } ``` or ``` { ... } ```
      2. The outermost bare ``{ ... }`` in the text.

    Returns the raw JSON string (not parsed), or None if nothing is found.
    """
    if not text:
        return None

    # Fenced block — greedy match on the outermost braces inside the fence
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)

    # Bare outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]

    return None


def parse_json_block(text: str) -> dict | None:
    """Extract and parse the first JSON object from agent output.

    Returns the parsed dict, or None if extraction or parsing fails.
    """
    raw = extract_json_block(text)
    if raw is None:
        return None
    try:
        result = json.loads(raw.strip())
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        logger.debug("JSON parse failed: %s … (truncated)", text[:120])
        return None
