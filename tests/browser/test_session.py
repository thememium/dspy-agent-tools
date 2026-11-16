"""Tests for browser session lifecycle, opening, and persistent profiles."""

import atexit
import threading
import time

import pytest
from pydoll.exceptions import ConnectionException, FailedToStartBrowser

from dspy_agent_tools.browser import BrowserSession, config
from dspy_agent_tools.browser import session as browser_session

from .fakes import FakeChrome, browser_module, snapshot_response


class TestSessionLifecycle:
    def test_first_call_starts_headless_chrome(self, session, fake_chrome):
        assert fake_chrome.instances == []
        result = session.browser("navigate", value="example.com")
        assert result["status"] == "ok"
        assert len(fake_chrome.instances) == 1
        chrome = fake_chrome.instances[0]
        assert chrome.started is True
        assert chrome.options.headless is True
        assert "--headless" in chrome.options.arguments
        assert "--window-size=1440,900" in chrome.options.arguments
        assert chrome.tab.navigations == [("https://example.com", 15)]
        assert result["url"] == "https://example.com"
        assert result["title"] == "Example"

    def test_calls_reuse_one_browser(self, session, fake_chrome):
        session.browser("navigate", value="https://a.example")
        session.browser("navigate", value="https://b.example")
        assert len(fake_chrome.instances) == 1
        assert len(fake_chrome.instances[0].tab.navigations) == 2

    def test_exit_hook_registered_after_start(self, session, monkeypatch):
        registered = []
        monkeypatch.setattr(browser_session, "_register_exit_hook", registered.append)
        session.browser("navigate", value="https://example.com")
        assert len(registered) == 1
        assert registered[0].__self__ is session

    def test_exit_hook_registered_once_across_restarts(self, session, monkeypatch):
        registered = []
        monkeypatch.setattr(browser_session, "_register_exit_hook", registered.append)
        session.browser("navigate", value="https://example.com")
        session.close()
        session.browser("navigate", value="https://example.com")
        assert len(registered) == 1

    def test_exit_hook_uses_threading_queue(self, monkeypatch):
        registered = []

        def callback():
            return None

        monkeypatch.setattr(threading, "_register_atexit", registered.append)
        browser_session._register_exit_hook(callback)
        assert registered == [callback]

    def test_exit_hook_falls_back_to_atexit(self, monkeypatch):
        monkeypatch.delattr(threading, "_register_atexit")
        registered = []

        def callback():
            return None

        monkeypatch.setattr(atexit, "register", registered.append)
        browser_session._register_exit_hook(callback)
        assert registered == [callback]

    def test_close_is_idempotent(self, session, fake_chrome):
        assert session.close() is False
        session.browser("navigate", value="https://example.com")
        assert session.close() is True
        assert fake_chrome.instances[0].stopped is True
        assert session.close() is False

    def test_close_swallows_stop_errors(self, session, fake_chrome):
        session.browser("navigate", value="https://example.com")
        chrome = fake_chrome.instances[0]

        def stop():
            raise RuntimeError("already gone")

        chrome.stop = stop
        assert session.close() is True

    def test_reset_without_browser(self, session):
        session._tab = object()
        session._browser = None
        session._reset()
        assert session._tab is None

    def test_reset_swallows_stop_errors(self, session, fake_chrome):
        session.browser("navigate", value="https://example.com")
        chrome = fake_chrome.instances[0]

        def stop():
            raise RuntimeError("already gone")

        chrome.stop = stop
        session._reset()
        assert session._tab is None

    def test_restarts_after_connection_error(self, session, fake_chrome):
        session.browser("navigate", value="https://example.com")
        first = fake_chrome.instances[0]
        first.tab.query_error = ConnectionException("socket closed")
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert first.stopped is True
        assert len(fake_chrome.instances) == 2

    def test_does_not_restart_on_element_error(self, session, fake_chrome):
        session.browser("navigate", value="https://example.com")
        fake_chrome.instances[0].tab.query_error = RuntimeError("boom")
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert len(fake_chrome.instances) == 1

    def test_start_failure_is_reported_without_retry(self, session, fake_chrome):
        fake_chrome.start_error = FailedToStartBrowser("no chrome")
        result = session.browser("navigate", value="https://example.com")
        assert result["status"] == "error"
        assert "FailedToStartBrowser" in result["error"]
        assert len(fake_chrome.instances) == 1

    def test_close_tool(self, default_session):
        assert browser_module.browser_close() == {
            "status": "ok",
            "action": "close",
            "closed": False,
        }
        browser_module.browser("navigate", value="https://example.com")
        result = browser_module.browser_close()
        assert result["closed"] is True

    def test_close_tool_reports_errors(self, default_session, monkeypatch):
        def close():
            raise RuntimeError("nope")

        monkeypatch.setattr(default_session, "close", close)
        result = browser_module.browser_close()
        assert result["status"] == "error"
        assert "nope" in result["error"]


class TestOpen:
    def test_navigates_then_snapshots(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        result = session.open("example.com")
        assert result["status"] == "ok"
        assert result["action"] == "open"
        assert result["url"] == "https://example.com/page"
        assert result["node_count"] == 0
        assert result["nodes"] == []
        assert "text" not in result
        assert session._tab.navigations == [("https://example.com", 15)]

    def test_navigation_error_skips_snapshot(self, session):
        result = session.open("   ")
        assert result["status"] == "error"
        assert result["action"] == "open"
        assert "navigate requires" in result["error"]
        assert session._tab is None

    def test_snapshot_error_keeps_open_action(self, session):
        session._get_tab()
        result = session.open("example.com")
        assert result["status"] == "error"
        assert result["action"] == "open"
        assert "unexpected shape" in result["error"]


class TestPersistentProfile:
    def test_user_data_dir_is_passed_to_chrome(self, fake_chrome, tmp_path):
        profile = tmp_path / "profile"
        session = BrowserSession(user_data_dir=str(profile))
        session._get_tab()
        options = FakeChrome.instances[0].options
        assert options is not None
        arguments = list(options.arguments)
        assert f"--user-data-dir={profile.resolve()}" in arguments
        assert profile.is_dir()
        assert session.user_data_dir == profile.resolve()
        session.close()

    def test_close_flushes_a_persistent_profile(
        self, fake_chrome, tmp_path, monkeypatch
    ):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        session = BrowserSession(user_data_dir=str(tmp_path / "profile"))
        session._get_tab()
        assert session.close() is True
        chrome = FakeChrome.instances[0]
        assert chrome.commands[0]["method"] == "Browser.close"
        assert slept == [config._PROFILE_FLUSH_S]

    def test_reset_flushes_a_persistent_profile(
        self, fake_chrome, tmp_path, monkeypatch
    ):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        session = BrowserSession(user_data_dir=str(tmp_path / "profile"))
        session._get_tab()
        session._reset()
        chrome = FakeChrome.instances[0]
        assert chrome.commands[0]["method"] == "Browser.close"
        assert slept == [config._PROFILE_FLUSH_S]

    def test_close_skips_the_flush_without_a_profile(
        self, session, fake_chrome, monkeypatch
    ):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        session._get_tab()
        assert session.close() is True
        assert not getattr(FakeChrome.instances[0], "commands", [])
        assert slept == []

    def test_flush_ignores_a_close_error(self, fake_chrome, tmp_path, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        FakeChrome.command_error = RuntimeError("already gone")
        session = BrowserSession(user_data_dir=str(tmp_path / "profile"))
        session._get_tab()
        assert session.close() is True
        assert slept == []

    def test_no_profile_by_default(self, session, fake_chrome):
        session._get_tab()
        options = FakeChrome.instances[0].options
        assert options is not None
        assert not any(
            argument.startswith("--user-data-dir=") for argument in options.arguments
        )


class TestConnectionAddress:
    WS_ADDRESS = "ws://127.0.0.1:9222/devtools/browser/abc"

    @pytest.mark.parametrize(
        "extra",
        [
            {"user_data_dir": "./profile"},
            {"cookie_state": "./cookies.json"},
            {"fingerprint": {"locale": "en-US"}},
        ],
    )
    def test_ws_address_rejects_profile_state(self, extra):
        with pytest.raises(ValueError, match="cannot be combined"):
            BrowserSession(ws_address=self.WS_ADDRESS, **extra)

    def test_ws_address_connects_instead_of_starting(self, fake_chrome):
        session = BrowserSession(ws_address=self.WS_ADDRESS)
        session._get_tab()
        chrome = fake_chrome.instances[0]
        assert chrome.connections == [self.WS_ADDRESS]
        assert chrome.started is True
        session.close()
        assert chrome.stopped is False
        assert chrome._impl.releases == 1
        assert chrome.runs == ["release-browser"]

    def test_reset_releases_a_connected_browser(self, fake_chrome):
        session = BrowserSession(ws_address=self.WS_ADDRESS)
        session._get_tab()
        session._reset()
        chrome = fake_chrome.instances[0]
        assert chrome._impl.releases == 1
        assert chrome.stopped is False


def test_fingerprint_is_applied_to_each_new_tab(fake_chrome):
    session = BrowserSession(fingerprint={"locale": "en-US"})
    try:
        session._get_tab()
        assert fake_chrome.instances[0].tab.fingerprints == [{"locale": "en-US"}]
    finally:
        session.close()
