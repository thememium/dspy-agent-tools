"""Tests for browser tab management."""

from types import SimpleNamespace

import pytest

from dspy_agent_tools.browser import errors, tabs

from .fakes import browser_module


class TestTabs:
    def test_list_reports_the_active_tab(self, session):
        result = session.tabs()
        assert result["status"] == "ok"
        assert result["action"] == "list"
        assert result["count"] == 1
        assert result["active"] == 0
        assert result["tabs"][0]["url"] == "https://example.com/page"
        assert result["tabs"][0]["title"] == "Example"

    def test_new_tab_with_url_becomes_active(self, session):
        result = session.tabs("new", "https://example.com/second")
        assert result["count"] == 2
        assert result["active"] == 1
        assert result["tabs"][1]["url"] == "https://example.com/second"
        assert session._tab is session._browser.tabs[1]

    def test_new_tab_without_url(self, session):
        result = session.tabs("new")
        assert result["count"] == 2
        assert session._browser.opened_urls == [""]

    def test_new_tab_rejects_blocked_scheme(self, session):
        result = session.tabs("new", "file:///etc/passwd")
        assert result["status"] == "error"
        assert "scheme" in result["error"]

    def test_switch_by_index(self, session):
        session.tabs("new", "https://example.com/second")
        result = session.tabs("switch", "0")
        assert result["active"] == 0
        assert result["tabs"][0]["active"] is True

    def test_switch_by_substring(self, session):
        session.tabs("new", "https://example.com/second")
        result = session.tabs("switch", "second")
        assert result["active"] == 1

    def test_switch_without_value_keeps_the_active_tab(self, session):
        session.tabs("new", "https://example.com/second")
        assert session.tabs("switch")["active"] == 1

    def test_switch_out_of_range(self, session):
        result = session.tabs("switch", "9")
        assert result["status"] == "error"
        assert "out of range" in result["error"]

    def test_switch_without_a_match(self, session):
        result = session.tabs("switch", "nothing")
        assert result["status"] == "error"
        assert "no tab matches" in result["error"]

    def test_switch_with_an_ambiguous_match(self, session):
        session.tabs("new", "https://example.com/second")
        result = session.tabs("switch", "example.com")
        assert result["status"] == "error"
        assert "2 tabs match" in result["error"]

    def test_close_the_active_tab(self, session):
        session.tabs("new", "https://example.com/second")
        result = session.tabs("close", "1")
        assert result["count"] == 1
        assert result["active"] == 0
        assert session._tab is session._browser.tabs[0]

    def test_close_the_last_tab_stops_the_browser(self, session):
        result = session.tabs("close")
        assert result["closed_all"] is True
        assert result["count"] == 0
        assert session._browser is None
        assert session._tab is None

    def test_unknown_action(self, session):
        result = session.tabs("duplicate")
        assert result["status"] == "error"
        assert "list, new, switch, or close" in result["error"]

    def test_resolving_against_no_tabs_is_an_error(self, session):
        with pytest.raises(errors._ToolError, match="no tabs are open"):
            tabs._resolve_tab(session, [], None)

    def test_tab_text_survives_a_dead_tab(self):
        dead = SimpleNamespace(
            current_url=lambda: (_ for _ in ()).throw(RuntimeError("gone")),
            title=lambda: "Fallback",
        )
        assert tabs._tab_text(dead) == "fallback"

    def test_module_tool_uses_default_session(self, default_session):
        assert browser_module.browser_tabs()["count"] == 1
