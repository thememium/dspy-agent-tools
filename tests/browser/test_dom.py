"""Tests for DOM helpers."""

import pytest

from dspy_agent_tools.browser import errors
from dspy_agent_tools.browser.dom import (
    _element,
    _require_actionable,
    _verify_typed,
    _xpath_literal,
)

from .fakes import FakeElement, FakeFrameDocument, FakeShadowRoot, FakeTab


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


class TestElementLookup:
    def test_ref_is_found_through_a_shadow_root(self):
        element = FakeElement()
        tab = FakeFrameDocument(
            shadow_roots=[FakeShadowRoot(), FakeShadowRoot(element)]
        )
        assert _element(tab, "@e1", 1) is element

    def test_ref_is_found_inside_nested_iframes(self):
        element = FakeElement()
        tab = FakeFrameDocument(
            frames=[FakeFrameDocument(frames=[FakeFrameDocument(body=element)])]
        )
        assert _element(tab, "@e1", 1) is element

    def test_iframe_search_stops_at_twelve_levels(self):
        inner = FakeFrameDocument()
        for _ in range(13):
            inner = FakeFrameDocument(frames=[inner])
        with pytest.raises(errors._ToolError, match="No element matched"):
            _element(inner, "@e1", 1)


def test_failed_shadow_hit_test_keeps_the_element_blocked():
    element = FakeElement()
    element.on_top = False
    element.script_error = RuntimeError("no hit testing here")
    with pytest.raises(errors._ToolError, match="covered by another element"):
        _require_actionable(element, "click")


def test_missing_shadow_root_support_falls_back_to_iframes():
    tab = FakeTab()
    tab.query_result = None
    with pytest.raises(errors._ToolError, match="No element matched"):
        _element(tab, "@e1", 1)
