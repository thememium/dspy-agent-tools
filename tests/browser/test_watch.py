"""Tests for dialogs, diagnostics, and tab change reporting."""

import threading
import time
from typing import Any

import pytest
from pydoll.exceptions import ConnectionException
from pydoll.protocol.runtime.events import RuntimeEvent

from dspy_agent_tools.browser import BrowserSession, config, watch

from .fakes import (
    FakeChrome,
    FakeTab,
    fire_console,
    fire_dialog,
    fire_error,
    snapshot_response,
    watch_callback,
)


class TestDialogs:
    def test_auto_dismissed_by_default(self, session):
        tab = session._get_tab()
        fire_dialog(tab)
        result = session.browser("scroll")
        assert result["dialog"] == {
            "type": "confirm",
            "message": "Are you sure?",
            "handled": "dismissed",
        }
        assert tab.dialogs == [(False, None)]

    def test_accept_policy_confirms(self, fake_chrome):
        session = BrowserSession(dialog_policy="accept")
        tab = session._get_tab()
        fire_dialog(tab)
        result = session.browser("scroll")
        assert result["dialog"]["handled"] == "accepted"
        assert tab.dialogs == [(True, None)]
        session.close()

    def test_accept_policy_fills_prompt(self, fake_chrome):
        session = BrowserSession(dialog_policy="accept")
        tab = session._get_tab()
        fire_dialog(tab, kind="prompt", prompt="default text")
        result = session.browser("scroll")
        assert result["dialog"]["default_prompt"] == "default text"
        assert tab.dialogs == [(True, "default text")]
        session.close()

    def test_alert_without_prompt_text(self, fake_chrome):
        session = BrowserSession(dialog_policy="accept")
        tab = session._get_tab()
        fire_dialog(tab, kind="alert")
        session.browser("scroll")
        assert tab.dialogs == [(True, None)]
        session.close()

    def test_dialog_reported_once(self, session):
        tab = session._get_tab()
        fire_dialog(tab)
        assert "dialog" in session.browser("scroll")
        assert "dialog" not in session.browser("scroll")

    def test_handle_failure_is_reported(self, session):
        tab = session._get_tab()

        def broken(**_kwargs):
            raise RuntimeError("no dialog")

        tab.handle_dialog = broken
        fire_dialog(tab)
        result = session.browser("scroll")
        assert result["dialog"]["handled"] == "failed: RuntimeError: no dialog"

    def test_take_waits_for_a_dialog_answer(self, session):
        watch = session._watch
        answering = threading.Event()
        released = threading.Event()
        entered = threading.Event()
        result = {}

        def responder(_dialog):
            answering.set()
            released.wait(5)
            return "dismissed"

        def drain():
            entered.set()
            result["dialog"] = watch.take()[2]

        watch.on_dialog({"params": {"type": "alert", "message": "hi"}}, responder)
        assert answering.wait(5)

        reader = threading.Thread(target=drain)
        reader.start()
        assert entered.wait(5)
        time.sleep(0.05)  # a drain that did not wait would be done by now
        assert reader.is_alive()
        released.set()
        reader.join(5)
        assert not reader.is_alive()
        assert result["dialog"]["handled"] == "dismissed"

    def test_open_keeps_navigate_diagnostics(self, session):
        tab = session._get_tab()
        tab.script_response = snapshot_response([])
        navigate = tab.go_to

        def go_to(url, timeout=300):
            navigate(url, timeout)
            fire_console(tab, {"value": "during navigate"})
            fire_dialog(tab)

        tab.go_to = go_to
        result = session.open("https://example.com/page")
        assert result["action"] == "open"
        assert result["console"] == [{"level": "log", "text": "during navigate"}]
        assert result["dialog"]["handled"] == "dismissed"

    def test_invalid_policy_is_rejected(self):
        policy: Any = "pause"
        with pytest.raises(ValueError, match="dialog_policy"):
            BrowserSession(dialog_policy=policy)


class TestDiagnostics:
    def test_console_attached_to_next_action(self, session):
        session._get_tab()
        fire_console(session._tab, {"value": "hello"}, {"type": "object"})
        result = session.browser("scroll")
        assert result["console"] == [{"level": "log", "text": "hello <object>"}]

    def test_console_reported_once(self, session):
        session._get_tab()
        fire_console(session._tab, {"value": "hello"})
        assert "console" in session.browser("scroll")
        assert "console" not in session.browser("scroll")

    def test_repeated_console_is_collapsed(self, session):
        session._get_tab()
        for _ in range(3):
            fire_console(session._tab, {"value": "tick"}, level="warning")
        result = session.browser("scroll")
        assert result["console"] == [{"level": "warning", "text": "tick", "count": 3}]

    def test_page_errors_attached(self, session):
        session._get_tab()
        fire_error(session._tab)
        result = session.browser("scroll")
        assert result["errors"] == [{"text": "Uncaught TypeError: boom"}]

    def test_empty_console_args_are_ignored(self, session):
        session._get_tab()
        fire_console(session._tab)
        assert "console" not in session.browser("scroll")

    def test_empty_exception_is_ignored(self, session):
        session._get_tab()
        watch_callback(session._tab, RuntimeEvent.EXCEPTION_THROWN)({"params": {}})
        assert "errors" not in session.browser("scroll")

    def test_bad_events_are_logged_not_raised(self, session):
        session._get_tab()
        watch = session._watch
        watch.on_console(None)
        watch.on_exception(None)
        assert session.browser("scroll")["status"] == "ok"

    def test_diagnostics_attach_to_errors(self, session):
        session._get_tab()
        fire_console(session._tab, {"value": "before the failure"})
        session._tab.query_result = None
        result = session.browser("click", target="#missing")
        assert result["status"] == "error"
        assert result["console"][0]["text"] == "before the failure"

    def test_watch_failure_does_not_block_the_session(self, session, monkeypatch):
        def start_with_failing_events(self):
            self.tab.events_error = RuntimeError("no events")
            return self.tab

        monkeypatch.setattr(FakeChrome, "start", start_with_failing_events)
        assert session.browser("scroll")["status"] == "ok"
        assert session._tab is not None

    def test_watch_is_reinstalled_after_a_reconnect(self, session):
        first_tab = session._get_tab()
        first_watch = session._watch

        def lost(_tab):
            raise ConnectionException("lost")

        with pytest.raises(ConnectionException):
            session._run(lost)
        assert session._watch is not first_watch
        assert session._tab is not first_tab
        assert session._tab.runtime_events == 1  # installed on the new tab

    def test_console_text_variants(self):
        text = watch._console_text
        assert text([{"value": "hi"}]) == "hi"
        assert text([{"value": None}]) == "null"
        assert text([{"value": {"a": 1}}]) == '{"a": 1}'
        assert text([{"description": "Error: x"}]) == "Error: x"
        assert text([{"type": "object"}]) == "<object>"
        assert text([5]) == "5"
        assert text([{"value": True}]) == "true"
        assert text("not a list") == ""
        assert len(text([{"value": "x" * 400}])) == 300

    def test_compress_keeps_the_tail(self):
        entries = [{"text": f"line {index}"} for index in range(12)]
        compressed = watch._compress(entries)
        assert len(compressed) == config._MAX_CONSOLE_ITEMS
        assert compressed[-1]["text"] == "line 11"
        assert "count" not in compressed[-1]


class TestTabChangeReporting:
    def test_click_reports_a_new_tab(self, session):
        session._get_tab()
        chrome = session._browser
        assert chrome is not None
        original = chrome.get_opened_tabs
        calls = []

        def counting():
            calls.append(1)
            if len(calls) == 1:
                return original()
            return [*original(), FakeTab()]

        chrome.get_opened_tabs = counting
        result = session.browser("click", target="#go")
        assert result["tabs"] == 2
        assert result["tabs_changed"] == 1
        assert "browser_tabs" in result["warning"]

    def test_click_reports_a_closed_tab(self, session):
        session.tabs("new")
        chrome = session._browser
        assert chrome is not None
        original = chrome.get_opened_tabs
        calls = []

        def counting():
            calls.append(1)
            return original() if len(calls) == 1 else original()[:1]

        chrome.get_opened_tabs = counting
        result = session.browser("click", target="#go")
        assert result["tabs_changed"] == -1
        assert "browser_tabs" not in result.get("warning", "")

    def test_tab_count_is_skipped_for_other_actions(self, session):
        session._get_tab()
        chrome = session._browser
        assert chrome is not None
        chrome.get_opened_tabs = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        assert session.browser("scroll")["status"] == "ok"

    def test_tab_count_failure_is_ignored(self, session):
        session._get_tab()
        chrome = session._browser
        assert chrome is not None
        chrome.get_opened_tabs = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert "tabs" not in result
