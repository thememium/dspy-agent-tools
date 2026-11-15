"""Tests for DOM helpers."""

import pytest

from dspy_agent_tools.browser.dom import _xpath_literal


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
