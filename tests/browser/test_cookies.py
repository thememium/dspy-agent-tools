"""Tests for browser cookies and persisted cookie state."""

import json

from dspy_agent_tools.browser import BrowserSession, cookies

from .fakes import FakeChrome, browser_module


class TestCookies:
    def test_get(self, session):
        result = session.cookies()
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["cookies"][0]["name"] == "sid"

    def test_get_filters_by_name(self, session):
        assert session.cookies("get", "nope")["count"] == 0
        assert session.cookies("get", "example.com")["count"] == 1

    def test_set_from_json_scopes_to_the_page(self, session):
        result = session.cookies("set", '[{"name": "a", "value": "1"}]')
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert session._tab.set_cookie_calls == [
            [{"name": "a", "value": "1", "url": "https://example.com/page"}]
        ]

    def test_set_from_a_cookie_header(self, session):
        result = session.cookies("set", "a=1; b=2")
        assert result["count"] == 2
        assert [c["name"] for c in session._tab.set_cookie_calls[0]] == ["a", "b"]

    def test_set_keeps_an_explicit_domain(self, session):
        session.cookies("set", '[{"name": "a", "value": "1", "domain": "e.com"}]')
        assert "url" not in session._tab.set_cookie_calls[0][0]

    def test_set_rejects_bad_json(self, session):
        result = session.cookies("set", "[{oops")
        assert result["status"] == "error"
        assert "not valid JSON" in result["error"]

    def test_set_rejects_an_empty_list(self, session):
        result = session.cookies("set", '{"cookies": []}')
        assert result["status"] == "error"
        assert "list of cookie objects" in result["error"]

    def test_set_rejects_a_cookie_without_a_name(self, session):
        result = session.cookies("set", '[{"value": "1"}]')
        assert result["status"] == "error"
        assert "name and a value" in result["error"]

    def test_set_rejects_an_empty_header(self, session):
        result = session.cookies("set", "; ;")
        assert result["status"] == "error"
        assert "name=value" in result["error"]

    def test_set_requires_a_value(self, session):
        result = session.cookies("set")
        assert result["status"] == "error"
        assert "Cookie header" in result["error"]

    def test_clear(self, session):
        assert session.cookies("clear")["status"] == "ok"
        assert session._tab.cookies == []

    def test_save_and_load(self, session, tmp_path):
        target = tmp_path / "state.json"
        saved = session.cookies("save", str(target))
        assert saved["status"] == "ok"
        assert saved["count"] == 1
        assert target.exists()

        session._tab.set_cookie_calls.clear()
        loaded = session.cookies("load", str(target))
        assert loaded["status"] == "ok"
        assert loaded["count"] == 1
        assert session._tab.set_cookie_calls[0][0]["name"] == "sid"

    def test_save_requires_a_path(self, session):
        result = session.cookies("save")
        assert result["status"] == "error"
        assert "value=path" in result["error"]

    def test_load_requires_a_path(self, session):
        result = session.cookies("load")
        assert result["status"] == "error"
        assert "value=path" in result["error"]

    def test_load_reports_a_missing_file(self, session, tmp_path):
        result = session.cookies("load", str(tmp_path / "missing.json"))
        assert result["status"] == "error"
        assert "could not read" in result["error"]

    def test_load_reports_bad_json(self, session, tmp_path):
        target = tmp_path / "bad.json"
        target.write_text("nope", encoding="utf-8")
        result = session.cookies("load", str(target))
        assert result["status"] == "error"
        assert "not valid JSON" in result["error"]

    def test_unknown_action(self, session):
        result = session.cookies("export")
        assert result["status"] == "error"
        assert "get, set, clear, save, or load" in result["error"]

    def test_cookie_records_ignore_junk(self):
        assert cookies._cookie_records("nope") == []
        assert cookies._cookie_records([{"name": "a"}, 5]) == [{"name": "a"}]

    def test_module_tool_uses_default_session(self, default_session):
        assert browser_module.browser_cookies()["count"] == 1


class TestCookieState:
    def write_state(self, target, cookies, url="https://example.com/page"):
        target.write_text(
            json.dumps({"url": url, "cookies": cookies}), encoding="utf-8"
        )

    def test_loaded_at_start(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        self.write_state(
            target, [{"name": "a", "value": "1", "domain": "example.com", "path": "/"}]
        )
        session = BrowserSession(cookie_state=str(target))
        tab = session._get_tab()
        assert tab.set_cookie_calls == [
            [{"name": "a", "value": "1", "domain": "example.com", "path": "/"}]
        ]
        session.close()

    def test_unscoped_cookie_uses_the_saved_url(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        self.write_state(target, [{"name": "b", "value": "2"}])
        session = BrowserSession(cookie_state=str(target))
        tab = session._get_tab()
        assert tab.set_cookie_calls == [
            [{"name": "b", "value": "2", "url": "https://example.com/page"}]
        ]
        session.close()

    def test_missing_file_is_ignored(self, fake_chrome, tmp_path):
        session = BrowserSession(cookie_state=str(tmp_path / "missing.json"))
        tab = session._get_tab()
        assert tab.set_cookie_calls == []
        session.close()

    def test_corrupt_file_is_ignored(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("{oops", encoding="utf-8")
        session = BrowserSession(cookie_state=str(target))
        tab = session._get_tab()
        assert tab.set_cookie_calls == []
        session.close()

    def test_empty_state_is_ignored(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        self.write_state(target, [])
        session = BrowserSession(cookie_state=str(target))
        tab = session._get_tab()
        assert tab.set_cookie_calls == []
        session.close()

    def test_load_failure_does_not_stop_startup(
        self, fake_chrome, tmp_path, monkeypatch
    ):
        target = tmp_path / "state.json"
        self.write_state(target, [{"name": "a", "value": "1", "domain": "e.com"}])

        def start_with_failing_cookies(self):
            self.tab.set_cookie_error = RuntimeError("bad domain")
            return self.tab

        monkeypatch.setattr(FakeChrome, "start", start_with_failing_cookies)
        session = BrowserSession(cookie_state=str(target))
        tab = session._get_tab()
        assert tab.set_cookie_calls == []
        assert session.cookie_state == target.resolve()
        session.close()

    def test_saved_on_close(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        session = BrowserSession(cookie_state=str(target))
        session._get_tab()
        assert session.close() is True
        state = json.loads(target.read_text(encoding="utf-8"))
        assert state["url"] == "https://example.com/page"
        assert state["cookies"][0]["name"] == "sid"

    def test_saved_after_set(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        session = BrowserSession(cookie_state=str(target))
        session.cookies("set", "a=1")
        state = json.loads(target.read_text(encoding="utf-8"))
        assert state["cookies"][0]["name"] == "sid"
        session.close()

    def test_saved_after_clear(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        session = BrowserSession(cookie_state=str(target))
        session.cookies("clear")
        state = json.loads(target.read_text(encoding="utf-8"))
        assert state["cookies"] == []
        session.close()

    def test_saved_on_reset(self, fake_chrome, tmp_path):
        target = tmp_path / "state.json"
        session = BrowserSession(cookie_state=str(target))
        session._get_tab()
        session._reset()
        assert (
            json.loads(target.read_text(encoding="utf-8"))["cookies"][0]["name"]
            == "sid"
        )

    def test_save_failure_does_not_raise(self, fake_chrome, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        session = BrowserSession(cookie_state=str(blocker / "state.json"))
        session._get_tab()
        assert session.close() is True

    def test_browser_forwards_cookie_state(self, tmp_path):
        target = tmp_path / "state.json"
        tools = browser_module.Browser(cookie_state=str(target))
        try:
            assert tools.session.cookie_state == target.resolve()
        finally:
            tools.close()
