"""Tests for DOM helpers."""

import pytest

from dspy_agent_tools.browser.dom import _verify_typed, _xpath_literal


def test_contenteditable_verification_requires_exact_text():
    assert (
        _verify_typed({"value": None, "text": "prefix hello"}, "hello")["verified"]
        is False
    )
    assert _verify_typed({"value": None, "text": "hello"}, "hello")["verified"] is True


class TestXPathLiteral:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("plain", "'plain'"),
            ("it's", '"it\'s"'),
            ('it\'s "quoted"', "concat('it', \"'\", 's \"quoted\"')"),
        ],
    )
    def test_xpath_literal(self, text, expected):
        assert _xpath_literal(text) == expected
