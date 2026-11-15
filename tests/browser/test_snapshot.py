"""Tests for browser snapshots and JavaScript evaluation."""

import pytest

from dspy_agent_tools.browser.snapshot import _render_nodes

from .fakes import snapshot_response


class TestSnapshot:
    def test_structured_nodes_and_text(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response(
            [
                {
                    "ref": "@e1",
                    "role": "link",
                    "name": "Docs",
                    "tag": "a",
                    "depth": 0,
                    "href": "/docs",
                },
                {
                    "ref": "@e2",
                    "role": "textbox",
                    "name": "",
                    "tag": "input",
                    "depth": 25,
                    "value": "",
                },
                {
                    "ref": "@e3",
                    "role": "checkbox",
                    "name": "Remember",
                    "tag": "input",
                    "depth": 1,
                    "checked": True,
                    "disabled": True,
                },
                {
                    "ref": "@e4",
                    "role": "textbox",
                    "name": "Name",
                    "tag": "input",
                    "depth": 1,
                    "value": "Ada",
                },
            ],
            truncated=True,
        )
        result = session.snapshot(format="both")
        assert result["status"] == "ok"
        assert result["node_count"] == 4
        assert result["truncated"] is True
        assert result["nodes"][0]["href"] == "/docs"
        assert result["text"] == (
            '@e1 link "Docs" href="/docs"\n'
            + "  " * 20
            + "@e2 textbox\n"
            + '  @e3 checkbox "Remember" checked disabled\n'
            + '  @e4 textbox "Name" value="Ada"'
        )

    def test_default_format_is_nodes_only(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        result = session.snapshot()
        assert "nodes" in result
        assert "text" not in result
        assert result["node_count"] == 0

    def test_text_format_omits_nodes(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        result = session.snapshot(format="text")
        assert "nodes" not in result
        assert result["text"] == ""

    def test_invalid_format(self, session):
        result = session.snapshot(format="html")
        assert result["status"] == "error"
        assert "format must be" in result["error"]

    def test_max_nodes_is_clamped(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        session.snapshot(max_nodes=5000)
        assert "const MAX_NODES = 1000;" in session._tab.scripts[-1][0]
        session.snapshot(max_nodes=0)
        assert "const MAX_NODES = 1;" in session._tab.scripts[-1][0]

    def test_max_nodes_accepts_numeric_string(self, session):
        session._get_tab()
        session.snapshot(max_nodes="250")
        assert "const MAX_NODES = 250;" in session._tab.scripts[-1][0]

    def test_max_nodes_rejects_text(self, session):
        result = session.snapshot(max_nodes="many")
        assert result["status"] == "error"
        assert "max_nodes" in result["error"]

    def test_unexpected_snapshot_shape(self, session):
        session._get_tab()
        session._tab.script_response = {"result": {"result": {"value": []}}}
        result = session.snapshot()
        assert result["status"] == "error"
        assert "unexpected shape" in result["error"]

    def test_snapshot_browser_error(self, session):
        session._get_tab()
        session._tab.script_error = RuntimeError("boom")
        result = session.snapshot()
        assert result["status"] == "error"
        assert "boom" in result["error"]


class TestEvaluate:
    def test_returns_value(self, session):
        session._get_tab()
        session._tab.script_response = {
            "result": {"result": {"type": "string", "value": "Example"}}
        }
        result = session.evaluate("return document.title")
        assert result == {
            "status": "ok",
            "action": "evaluate",
            "result": "Example",
            "type": "string",
            "url": "https://example.com/page",
            "title": "Example",
        }

    def test_undefined_returns_none(self, session):
        session._get_tab()
        session._tab.script_response = {"result": {"result": {"type": "undefined"}}}
        result = session.evaluate("return undefined")
        assert result["result"] is None
        assert result["type"] == "undefined"

    def test_unserializable_value(self, session):
        session._get_tab()
        session._tab.script_response = {
            "result": {"result": {"type": "number", "unserializableValue": "NaN"}}
        }
        result = session.evaluate("return NaN")
        assert result["result"] == "NaN"

    def test_description_fallback(self, session):
        session._get_tab()
        session._tab.script_response = {
            "result": {
                "result": {
                    "type": "object",
                    "description": "Object",
                }
            }
        }
        result = session.evaluate("return {}")
        assert result["result"] == "Object"

    def test_javascript_exception(self, session):
        session._get_tab()
        session._tab.script_response = {
            "result": {
                "result": {"type": "object"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "Error: boom"},
                },
            }
        }
        result = session.evaluate("throw new Error('boom')")
        assert result["status"] == "error"
        assert "JavaScript error: Error: boom" in result["error"]

    def test_exception_without_description(self, session):
        session._get_tab()
        session._tab.script_response = {
            "result": {
                "result": {"type": "object"},
                "exceptionDetails": {"text": "Uncaught"},
            }
        }
        result = session.evaluate("throw 1")
        assert result["status"] == "error"
        assert "Uncaught" in result["error"]

    def test_requires_script(self, session):
        session._get_tab()
        result = session.evaluate("   ")
        assert result["status"] == "error"
        assert "non-empty" in result["error"]

    def test_numeric_script_is_rejected(self, session):
        session._get_tab()
        result = session.evaluate(2)
        assert result["status"] == "error"
        assert "non-empty" in result["error"]

    @pytest.mark.parametrize("response", [[], {"result": []}])
    def test_unexpected_response(self, session, response):
        session._get_tab()
        session._tab.script_response = response
        result = session.evaluate("return 1")
        assert result["status"] == "error"
        assert "unexpected script response" in result["error"]


class TestRenderNodes:
    def test_render_empty_nodes(self):
        assert _render_nodes([]) == ""
