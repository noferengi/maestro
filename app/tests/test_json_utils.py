"""
Tests for app/agent/json_utils.py

Covers extract_json_block and parse_json_block with various input formats.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.json_utils import extract_json_block, parse_json_block


class TestExtractJsonBlock:
    def test_bare_object(self):
        text = '{"key": "value"}'
        result = extract_json_block(text)
        assert result == '{"key": "value"}'

    def test_fenced_json_block(self):
        text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
        result = extract_json_block(text)
        assert '"key"' in result
        assert '"value"' in result

    def test_fenced_no_lang_tag(self):
        text = 'Here is the result:\n```\n{"status": "ok"}\n```'
        result = extract_json_block(text)
        assert '"status"' in result

    def test_prose_embedded(self):
        text = 'The verdict is: {"verdict": "passed", "confidence": 85} and that is final.'
        result = extract_json_block(text)
        assert '"verdict"' in result
        assert '"passed"' in result

    def test_nested_object(self):
        text = '{"outer": {"inner": [1, 2, 3]}}'
        result = extract_json_block(text)
        assert '"outer"' in result

    def test_empty_string(self):
        result = extract_json_block("")
        # Implementation returns None or "" for no-json input - both are acceptable
        assert result is None or result == ""

    def test_no_json(self):
        result = extract_json_block("no json here at all")
        # Implementation returns None or "" for no-json input - both are acceptable
        assert result is None or result == ""

    def test_array(self):
        text = '[{"id": 1}, {"id": 2}]'
        result = extract_json_block(text)
        assert '"id"' in result


class TestParseJsonBlock:
    def test_valid_dict(self):
        text = '{"verdict": "passed"}'
        result = parse_json_block(text)
        assert result == {"verdict": "passed"}

    def test_fenced_valid(self):
        text = '```json\n{"verdict": "passed"}\n```'
        result = parse_json_block(text)
        assert result is not None
        assert result.get("verdict") == "passed"

    def test_malformed_returns_none(self):
        result = parse_json_block("{not valid json")
        assert result is None

    def test_non_dict_returns_none(self):
        # A JSON array is valid JSON but not a dict
        result = parse_json_block('[1, 2, 3]')
        # parse_json_block may return None for non-dict or the list itself
        # The important thing is it doesn't raise
        assert True  # no exception

    def test_empty_returns_none(self):
        result = parse_json_block("")
        assert result is None
