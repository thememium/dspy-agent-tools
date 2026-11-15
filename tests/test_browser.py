"""Tests for dspy_agent_tools.browser with a fake pydoll browser."""

import atexit
import base64
import builtins
import importlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import dspy
import pytest
from pydoll.constants import ScrollPosition
from pydoll.exceptions import (
    ConnectionException,
    FailedToStartBrowser,
    TimeoutException,
)
from pydoll.protocol.network.events import NetworkEvent
from pydoll.protocol.runtime.events import RuntimeEvent
from pydoll.sync import Key, PageEvent

from dspy_agent_tools.browser import (
    BrowserSession,
    _pydoll,
    actions,
    coerce,
    config,
    cookies,
    dom,
    errors,
    screenshot,
    tabs,
    watch,
)
from dspy_agent_tools.browser import (
    session as browser_session,
)
from dspy_agent_tools.browser import (
    tools as tools_module,
)
from dspy_agent_tools.browser.dom import _xpath_literal
from dspy_agent_tools.browser.snapshot import _render_nodes

browser_module = importlib.import_module("dspy_agent_tools.browser")


class FakeElement:
    def __init__(self):
        self.click_calls = 0
        self.js_click_calls = 0
        self.clear_calls = 0
        self.typed = []
        self.inserted = []
        self.focus_calls = 0
        self.hover_calls = 0
        self.scripts = []
        self.click_error = None
        self.js_click_error = None
        self.type_error = None
        self.insert_error = None
        self.visible = True
        self.enabled = True
        self.on_top = True
        self.editable = True
        self.scroll_calls = 0
        self.field_state = {
            "tag": "input",
            "value": None,
            "text": None,
            "type": "text",
            "disabled": False,
            "readonly": False,
        }
        self.field_state_error = None
        self.mirror_typing = True

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def is_on_top(self):
        return self.on_top

    def is_editable(self):
        return self.editable

    def scroll_into_view(self):
        self.scroll_calls += 1

    def click(self):
        self.click_calls += 1
        if self.click_error:
            raise self.click_error

    def click_using_js(self):
        self.js_click_calls += 1
        if self.js_click_error:
            raise self.js_click_error

    def clear(self):
        self.clear_calls += 1

    def type_text(self, value):
        if self.type_error:
            raise self.type_error
        self.typed.append(value)
        if self.mirror_typing:
            self.field_state["value"] = value

    def focus(self):
        self.focus_calls += 1

    def insert_text(self, value):
        if self.insert_error:
            raise self.insert_error
        self.inserted.append(value)
        self.field_state["value"] = value

    def hover(self):
        self.hover_calls += 1

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if "readOnly" in script:
            if self.field_state_error:
                raise self.field_state_error
            return {"result": {"result": {"value": self.field_state}}}
        return {"result": {"result": {"type": "string", "value": "selected"}}}


class FakeKeyboard:
    def __init__(self):
        self.presses = []
        self.hotkeys = []

    def press(self, key):
        self.presses.append(key)

    def hotkey(self, *keys):
        self.hotkeys.append(keys)


class FakeScroll:
    def __init__(self):
        self.calls = []

    def by(self, position, distance, smooth=True):
        self.calls.append(("by", position, distance, smooth))

    def to_top(self, smooth=True):
        self.calls.append(("top", smooth))

    def to_bottom(self, smooth=True):
        self.calls.append(("bottom", smooth))


class FakeTab:
    def __init__(self, element=None):
        self.element = element or FakeElement()
        self.target_id = "target-1"
        self.closed = False
        self.cookies = [
            {"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}
        ]
        self.set_cookie_calls = []
        self.set_cookie_error = None
        self.network_body = '{"ok": true}'
        self.url = "https://example.com/page"
        self.page_title = "Example"
        self.navigations = []
        self.queries = []
        self.scripts = []
        self.keyboard = FakeKeyboard()
        self.scroll = FakeScroll()
        self.refresh_calls = 0
        self.screenshot_calls = []
        self.query_error = None
        self.query_result = self.element
        self.script_response = None
        self.script_error = None
        self.current_url_error = None
        self.title_error = None
        self.screenshot_error = None
        self.network_idle_calls = []
        self.url_wait_calls = []
        self.wait_error = None
        self.page_events = 0
        self.runtime_events = 0
        self.network_events = 0
        self.events_error = None
        self.callbacks = {}
        self.removed_callbacks = []
        self.commands = []
        self.command_error = None
        self.remove_callback_error = None
        self._next_callback_id = 0
        self.probe_responses = []
        self.probe_error = None
        self.wait_script_calls = []
        self.wait_script_error = None
        self.dialogs = []

    def go_to(self, url, timeout=300):
        self.navigations.append((url, timeout))
        self.url = url

    def current_url(self):
        if self.current_url_error:
            raise self.current_url_error
        return self.url

    def title(self):
        if self.title_error:
            raise self.title_error
        return self.page_title

    def wait_for_network_idle(self, idle_time=0.5, timeout=30):
        self.network_idle_calls.append((idle_time, timeout))
        if self.wait_error:
            raise self.wait_error

    def wait_for_url(self, url, timeout=30):
        self.url_wait_calls.append((url, timeout))
        if self.wait_error:
            raise self.wait_error
        return self.url

    def query(self, expression, timeout=0, find_all=False, raise_exc=True):
        self.queries.append((expression, timeout, find_all, raise_exc))
        if self.query_error:
            raise self.query_error
        return self.query_result

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if self.script_error:
            raise self.script_error
        if "__dspyAgentProbe" in script:
            if self.probe_error:
                raise self.probe_error
            if self.probe_responses:
                value = self.probe_responses.pop(0)
            else:
                value = {"url": self.url, "mutations": 0}
            return {"result": {"result": {"value": value}}}
        if self.script_response is not None:
            return self.script_response
        return {"result": {"result": {"type": "undefined"}}}

    def refresh(self):
        self.refresh_calls += 1

    def take_screenshot(
        self, path=None, quality=100, beyond_viewport=False, as_base64=False
    ):
        self.screenshot_calls.append((path, quality, beyond_viewport, as_base64))
        if self.screenshot_error:
            raise self.screenshot_error
        if as_base64:
            return "ZmFrZQ=="
        assert path is not None
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image")

    def enable_page_events(self):
        self.page_events += 1
        if self.events_error:
            raise self.events_error

    def enable_runtime_events(self):
        self.runtime_events += 1
        if self.events_error:
            raise self.events_error

    def enable_network_events(self):
        self.network_events += 1
        if self.events_error:
            raise self.events_error

    def close(self):
        self.closed = True

    def get_cookies(self):
        return list(self.cookies)

    def set_cookies(self, cookies):
        if self.set_cookie_error:
            raise self.set_cookie_error
        self.set_cookie_calls.append([dict(cookie) for cookie in cookies])

    def delete_all_cookies(self):
        self.cookies = []

    def get_network_response_body(self, request_id):
        self.requested_body = request_id
        return self.network_body

    def wait_for_script(self, script, timeout=30):
        self.wait_script_calls.append((script, timeout))
        if self.wait_script_error:
            raise self.wait_script_error
        return True

    def handle_dialog(self, accept, prompt_text=None):
        self.dialogs.append((accept, prompt_text))

    def on(self, event_name, callback, temporary=False):
        self._next_callback_id += 1
        self.callbacks[self._next_callback_id] = (event_name, callback, temporary)
        return self._next_callback_id

    def remove_callback(self, callback_id):
        self.removed_callbacks.append(callback_id)
        if self.remove_callback_error:
            raise self.remove_callback_error
        self.callbacks.pop(callback_id, None)

    def execute_command(self, command, timeout=60):
        self.commands.append(command)
        if self.command_error:
            raise self.command_error
        return {"result": {}}


class FakeChrome:
    instances: ClassVar[list["FakeChrome"]] = []
    start_error = None
    command_error = None

    def __init__(self, options=None, connection_port=None):
        self.options = options
        self.started = False
        self.stopped = False
        self.tab = FakeTab()
        self.tabs = [self.tab]
        self.opened_urls = []
        FakeChrome.instances.append(self)

    def start(self):
        if FakeChrome.start_error:
            raise FakeChrome.start_error
        self.started = True
        return self.tab

    def new_tab(self, url="", browser_context_id=None):
        tab = FakeTab()
        tab.target_id = f"target-{len(self.tabs) + 1}"
        self.tabs.append(tab)
        self.opened_urls.append(url)
        if url:
            tab.go_to(url)
        return tab

    def get_opened_tabs(self):
        return [tab for tab in self.tabs if not tab.closed]

    def execute_command(self, command, timeout=60):
        self.commands = getattr(self, "commands", [])
        self.commands.append(command)
        if FakeChrome.command_error:
            raise FakeChrome.command_error

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_chrome(monkeypatch):
    _pydoll.require()
    FakeChrome.instances = []
    FakeChrome.start_error = None
    monkeypatch.setattr(_pydoll, "Chrome", FakeChrome)
    return FakeChrome


@pytest.fixture
def session(fake_chrome):
    session = BrowserSession()
    yield session
    BrowserSession.close(session)


@pytest.fixture
def default_session(monkeypatch, fake_chrome):
    session = BrowserSession()
    monkeypatch.setattr(tools_module, "_default_session", session)
    yield session
    BrowserSession.close(session)


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    state = {"returncode": 0, "stderr": "", "stdout": "", "write": True}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        state["concat"] = Path(command[command.index("-i") + 1]).read_text()
        if state["write"]:
            Path(command[-1]).write_bytes(b"fake-video")
        returncode = state["returncode"]
        stdout = state["stdout"]
        stderr = state["stderr"]
        assert isinstance(returncode, int)
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(coerce, "_ffmpeg_binary", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", run)
    return state, calls


def frame_event(data=b"frame", timestamp=1.0, session_id=1):
    params = {"data": base64.b64encode(data).decode(), "sessionId": session_id}
    if timestamp is not None:
        params["metadata"] = {"timestamp": timestamp}
    return {"params": params}


def screencast_callback(tab):
    for event, callback, _temporary in tab.callbacks.values():
        if event == PageEvent.SCREENCAST_FRAME:
            return callback
    raise AssertionError("no screencast callback registered")


def screencast_callback_id(tab):
    for callback_id, (event, _callback, _temporary) in tab.callbacks.items():
        if event == PageEvent.SCREENCAST_FRAME:
            return callback_id
    raise AssertionError("no screencast callback registered")


def snapshot_response(nodes, *, truncated=False):
    return {
        "result": {
            "result": {
                "value": {
                    "url": "https://example.com/page",
                    "title": "Example",
                    "nodes": nodes,
                    "truncated": truncated,
                }
            }
        }
    }


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


class TestNavigate:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("example.com", "https://example.com"),
            ("http://example.com", "http://example.com"),
            ("//example.com", "https://example.com"),
            ("localhost:8000", "http://localhost:8000"),
            ("127.0.0.1:9000", "http://127.0.0.1:9000"),
            ("data:text/html,hi", "data:text/html,hi"),
            ("about:blank", "about:blank"),
        ],
    )
    def test_url_normalization(self, session, value, expected):
        session.browser("navigate", value=value)
        assert session._tab.navigations[-1][0] == expected

    def test_file_url_blocked_by_default(self, session):
        result = session.browser("navigate", value="file:///tmp/a")
        assert result["status"] == "error"
        assert "scheme file" in result["error"]
        assert session._tab is None

    def test_allowed_schemes_can_include_file(self, fake_chrome):
        session = BrowserSession(allowed_schemes=("http", "https", "file"))
        try:
            result = session.browser("navigate", value="file:///tmp/a")
            assert result["status"] == "ok"
            assert session._tab is not None
            assert session._tab.navigations == [("file:///tmp/a", 15)]
        finally:
            session.close()

    def test_url_policy_can_reject_and_accept(self, fake_chrome):
        session = BrowserSession(url_policy=lambda url: "example.com" in url)
        try:
            rejected = session.browser("navigate", value="https://blocked.test")
            assert rejected["status"] == "error"
            assert "URL policy" in rejected["error"]
            accepted = session.browser("navigate", value="https://example.com")
            assert accepted["status"] == "ok"
            assert session._tab is not None
            assert session._tab.navigations == [("https://example.com", 15)]
        finally:
            session.close()

    def test_missing_url(self, session):
        result = session.browser("navigate")
        assert result["status"] == "error"
        assert "value=URL" in result["error"]

    def test_timeout_defaults_when_none(self, session):
        result = session.browser("navigate", value="https://example.com", timeout=None)
        assert result["status"] == "ok"
        assert session._tab.navigations[-1][1] == 15

    def test_timeout_rejects_zero(self, session):
        result = session.browser("navigate", value="https://example.com", timeout=0)
        assert result["status"] == "error"
        assert "greater than zero" in result["error"]

    def test_timeout_rejects_non_number(self, session):
        result = session.browser(
            "navigate", value="https://example.com", timeout="soon"
        )
        assert result["status"] == "error"
        assert "number of seconds" in result["error"]

    def test_page_info_failures_are_omitted(self, session):
        session.browser("navigate", value="https://example.com")
        tab = session._tab
        tab.current_url_error = RuntimeError("no url")
        tab.title_error = RuntimeError("no title")
        result = session.browser("navigate", value="https://example.com")
        assert result["status"] == "ok"
        assert "url" not in result
        assert "title" not in result


class TestActions:
    def test_click_by_ref(self, session):
        result = session.browser("click", target="@e3")
        assert result["status"] == "ok"
        assert result["method"] == "native"
        assert session._tab.queries[0][0] == '[data-dspy-agent-ref="e3"]'
        assert session._tab.element.click_calls == 1
        assert session._tab.network_idle_calls == [(0.2, 2.0)]

    def test_click_falls_back_to_js(self, session):
        session._get_tab()
        element = session._tab.element
        element.click_error = RuntimeError("blocked")
        result = session.browser("click", target="#go")
        assert result["method"] == "js"
        assert element.js_click_calls == 1

    def test_click_reports_native_error_when_both_fail(self, session):
        session._get_tab()
        element = session._tab.element
        element.click_error = RuntimeError("blocked")
        element.js_click_error = RuntimeError("also blocked")
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "blocked" in result["error"]

    def test_click_requires_target(self, session):
        result = session.browser("click")
        assert result["status"] == "error"
        assert "requires target" in result["error"]

    @pytest.mark.parametrize(
        ("target", "selector"),
        [
            ("ref=e3", '[data-dspy-agent-ref="e3"]'),
            ("css=#main", "#main"),
            ("xpath=//button", "//button"),
            ("text=Sign in", "(//*[normalize-space(.) = 'Sign in'"),
            ("text*=Sign", "(//*[contains(normalize-space(.), 'Sign')"),
            (".card > button", ".card > button"),
        ],
    )
    def test_target_syntax(self, session, target, selector):
        session.browser("click", target=target)
        assert session._tab.queries[0][0].startswith(selector)

    @pytest.mark.parametrize(
        "target",
        ["@bad ref", "ref=", "ref=bad ref", "css=", "xpath=", "text=", "text*="],
    )
    def test_invalid_targets(self, session, target):
        result = session.browser("click", target=target)
        assert result["status"] == "error"

    def test_element_not_found(self, session):
        session._get_tab()
        session._tab.query_result = None
        result = session.browser("click", target="#missing")
        assert result["status"] == "error"
        assert "No element matched" in result["error"]

    def test_type_clears_then_types(self, session):
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["status"] == "ok"
        assert result["value_length"] == len("hi@example.com")
        assert result["method"] == "keys"
        assert session._tab.element.clear_calls == 1
        assert session._tab.element.typed == ["hi@example.com"]

    def test_type_falls_back_to_js_insertion(self, session):
        session._get_tab()
        element = session._tab.element
        element.type_error = RuntimeError("covered")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "ok"
        assert result["method"] == "js"
        assert element.focus_calls == 1
        assert element.inserted == ["hi"]

    def test_type_reports_original_error_when_fallback_fails(self, session):
        session._get_tab()
        element = session._tab.element
        element.type_error = RuntimeError("covered")
        element.insert_error = RuntimeError("not editable")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "error"
        assert "covered" in result["error"]

    def test_type_requires_value(self, session):
        result = session.browser("type", target="#email")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    def test_press_single_key(self, session):
        result = session.browser("press", value="Enter")
        assert result["status"] == "ok"
        assert session._tab.keyboard.presses == [Key.ENTER]

    def test_press_hotkey(self, session):
        session.browser("press", value="Control+A")
        assert session._tab.keyboard.hotkeys == [(Key.CONTROL, Key.A)]

    def test_press_three_key_hotkey(self, session):
        session.browser("press", value="Control+Shift+A")
        assert session._tab.keyboard.hotkeys == [(Key.CONTROL, Key.SHIFT, Key.A)]

    @pytest.mark.parametrize(
        ("value", "key"),
        [("Cmd", Key.META), ("Esc", Key.ESCAPE), ("a", Key.A), (" ", Key.SPACE)],
    )
    def test_press_aliases(self, session, value, key):
        session.browser("press", value=value)
        assert session._tab.keyboard.presses == [key]

    def test_press_cmd_alias_in_hotkey(self, session):
        session.browser("press", value="Cmd+C")
        assert session._tab.keyboard.hotkeys == [(Key.META, Key.C)]

    def test_press_unknown_key(self, session):
        result = session.browser("press", value="F13")
        assert result["status"] == "error"
        assert "Unknown key" in result["error"]

    def test_press_requires_value(self, session):
        result = session.browser("press")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    def test_press_rejects_too_many_keys(self, session):
        result = session.browser("press", value="A+B+C+D")
        assert result["status"] == "error"
        assert "up to three" in result["error"]

    def test_hover(self, session):
        result = session.browser("hover", target="#menu")
        assert result["status"] == "ok"
        assert session._tab.element.hover_calls == 1

    def test_select(self, session):
        result = session.browser("select", target="#country", value="ca")
        assert result["status"] == "ok"
        assert result["selected"] == "selected"
        script, kwargs = session._tab.element.scripts[0]
        assert "dispatchEvent" in script
        assert kwargs["arguments"] == [{"value": "ca"}]

    def test_select_requires_value(self, session):
        result = session.browser("select", target="#country")
        assert result["status"] == "error"
        assert "requires value" in result["error"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("down", ("by", ScrollPosition.DOWN, 600, False)),
            ("up", ("by", ScrollPosition.UP, 600, False)),
            ("top", ("top", False)),
            ("bottom", ("bottom", False)),
            ("250", ("by", ScrollPosition.DOWN, 250, False)),
            ("-250", ("by", ScrollPosition.UP, 250, False)),
        ],
    )
    def test_scroll(self, session, value, expected):
        result = session.browser("scroll", value=value)
        assert result["status"] == "ok"
        assert session._tab.scroll.calls == [expected]

    def test_scroll_zero(self, session):
        result = session.browser("scroll", value="0")
        assert result["status"] == "error"
        assert "cannot be zero" in result["error"]

    def test_scroll_invalid(self, session):
        result = session.browser("scroll", value="sideways")
        assert result["status"] == "error"
        assert "scroll value" in result["error"]

    @pytest.mark.parametrize("action", ["back", "forward"])
    def test_history(self, session, action):
        result = session.browser(action)
        assert result["status"] == "ok"
        assert session._tab.scripts[-1][0] == f"history.{action}()"
        assert len(session._tab.url_wait_calls) == 1
        idle_time, wait_timeout = session._tab.network_idle_calls[0]
        assert idle_time == 0.2
        assert 0.1 <= wait_timeout <= 2.0

    def test_reload(self, session):
        result = session.browser("reload")
        assert result["status"] == "ok"
        assert session._tab.refresh_calls == 1
        assert session._tab.network_idle_calls == [(0.2, 2.0)]

    def test_settle_errors_are_best_effort(self, session):
        session._get_tab()
        session._tab.wait_error = RuntimeError("timed out")
        assert (
            actions._settle(session._tab, 15, previous_url="https://example.com/old")
            is False
        )

    def test_wait(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        result = session.browser("wait", value="2.5")
        assert result["status"] == "ok"
        assert result["seconds"] == 2.5
        assert slept == [2.5]

    def test_wait_defaults(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", slept.append)
        result = session.browser("wait")
        assert result["seconds"] == 1.0
        assert slept == [1.0]

    def test_wait_rejects_negative(self, session):
        result = session.browser("wait", value="-1")
        assert result["status"] == "error"
        assert "cannot be negative" in result["error"]

    def test_wait_rejects_unknown_condition(self, session):
        result = session.browser("wait", value="soon")
        assert result["status"] == "error"
        assert "wait value must be" in result["error"]

    def test_numeric_value_is_text(self, session):
        result = session.browser("scroll", value=300)
        assert result["status"] == "ok"
        assert session._tab.scroll.calls == [("by", ScrollPosition.DOWN, 300, False)]

    def test_numeric_target_is_text(self, session):
        result = session.browser("click", target=2)
        assert result["status"] == "ok"
        assert session._tab.queries[0][0] == "2"

    def test_unknown_action(self, session):
        result = session.browser("teleport")
        assert result["status"] == "error"
        assert "Unknown action" in result["error"]


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


class TestScreenshot:
    def test_writes_file(self, session, tmp_path):
        target = tmp_path / "shot.png"
        result = session.screenshot(path=str(target), full_page=True, quality=50)
        assert result["status"] == "ok"
        assert result["path"] == str(target)
        assert result["format"] == "png"
        assert result["bytes"] == len(b"fake-image")
        assert target.read_bytes() == b"fake-image"
        assert session._tab.screenshot_calls == [(str(target), 50, True, False)]

    def test_quality_accepts_numeric_string(self, session, tmp_path):
        result = session.screenshot(path=str(tmp_path / "shot.jpg"), quality="42")
        assert result["status"] == "ok"
        assert session._tab.screenshot_calls[-1][1] == 42

    def test_adds_png_suffix(self, session, tmp_path):
        target = tmp_path / "shot"
        result = session.screenshot(path=str(target))
        assert result["path"] == str(target.with_suffix(".png"))

    def test_default_temp_path(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        result = session.screenshot()
        path = Path(result["path"])
        assert path.parent == tmp_path / "dspy-agent-tools"
        assert path.suffix == ".png"
        assert path.exists()

    def test_base64(self, session):
        result = session.screenshot(path="shot.jpg", as_base64=True)
        assert result["base64"] == "ZmFrZQ=="
        assert result["format"] == "jpeg"
        assert session._tab.screenshot_calls == [("shot.jpg", 80, False, True)]

    def test_quality_is_validated(self, session):
        result = session.screenshot(quality=101)
        assert result["status"] == "error"
        assert "quality" in result["error"]

    def test_quality_rejects_text(self, session):
        result = session.screenshot(quality="high")
        assert result["status"] == "error"
        assert "quality" in result["error"]

    def test_reports_browser_error(self, session):
        session._get_tab()
        session._tab.screenshot_error = RuntimeError("no screenshot")
        result = session.screenshot()
        assert result["status"] == "error"
        assert "no screenshot" in result["error"]


class TestRecording:
    def test_start_and_stop_encodes_video(self, session, tmp_path, fake_ffmpeg):
        state, calls = fake_ffmpeg
        result = session.record_start(
            path=str(tmp_path / "demo.MP4"),
            fps=10,
            quality=50,
            max_width=800,
            max_height=600,
        )
        assert result["status"] == "ok"
        assert Path(result["path"]) == tmp_path / "demo.mp4"
        assert result["fps"] == 10.0
        assert result["quality"] == 50
        assert result["max_width"] == 800
        assert result["max_height"] == 600
        tab = session._tab
        assert tab.page_events == 2  # session start plus the screencast
        assert tab.commands[0]["method"] == "Page.startScreencast"
        assert tab.commands[0]["params"] == {
            "format": "jpeg",
            "quality": 50,
            "maxWidth": 800,
            "maxHeight": 600,
            "everyNthFrame": 1,
        }

        callback = screencast_callback(tab)
        callback(frame_event(timestamp=1.0))
        callback(frame_event(b"second", timestamp=1.5))
        recorder = session._recorder
        assert recorder is not None
        assert len(recorder.frames) == 2
        assert all(frame_path.exists() for _stamp, frame_path in recorder.frames)
        assert [command["method"] for command in tab.commands] == [
            "Page.startScreencast",
            "Page.screencastFrameAck",
            "Page.screencastFrameAck",
        ]

        screencast_id = screencast_callback_id(tab)
        stopped = session.record_stop()
        assert stopped["status"] == "ok"
        assert stopped["frames"] == 2
        assert stopped["duration_s"] == 0.6
        assert stopped["format"] == "mp4"
        assert stopped["bytes"] == len(b"fake-video")
        assert Path(stopped["path"]).read_bytes() == b"fake-video"
        assert tab.removed_callbacks == [screencast_id]
        assert tab.commands[-1]["method"] == "Page.stopScreencast"
        assert session._recorder is None

        video_command = calls[0]
        assert "libx264" in video_command
        assert "-movflags" in video_command
        assert Path(video_command[-1]).name == "demo.part.mp4"
        assert "duration 0.500000" in state["concat"]
        assert "duration 0.100000" in state["concat"]

    def test_webm_uses_vp9(self, session, tmp_path, fake_ffmpeg):
        _state, calls = fake_ffmpeg
        assert session.record_start(path=str(tmp_path / "demo.webm"))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        result = session.record_stop()
        assert result["format"] == "webm"
        assert "libvpx-vp9" in calls[0]
        assert "-movflags" not in calls[0]

    def test_default_path_is_temp_mp4(self, session, fake_ffmpeg):
        result = session.record_start()
        assert result["status"] == "ok"
        assert Path(result["path"]).suffix == ".mp4"
        assert "dspy-agent-tools" in result["path"]

    def test_video_path_adds_mp4_suffix(self, tmp_path):
        assert coerce._video_path(str(tmp_path / "clip")).name == "clip.mp4"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"fps": "fast"}, "fps must"),
            ({"fps": 0}, "fps must"),
            ({"fps": 61}, "fps must"),
            ({"quality": 101}, "quality must"),
            ({"max_width": "wide"}, "max_width must"),
            ({"max_width": 0}, "max_width must"),
            ({"max_height": -1}, "max_height must"),
            ({"path": "demo.mov"}, ".mp4 or .webm"),
        ],
    )
    def test_start_rejects_invalid_options(
        self, session, tmp_path, fake_ffmpeg, kwargs, message
    ):
        if kwargs.get("path") == "demo.mov":
            kwargs["path"] = str(tmp_path / "demo.mov")
        result = session.record_start(**kwargs)
        assert result["status"] == "error"
        assert message in result["error"]

    def test_start_rejects_directory_path(self, session, tmp_path, fake_ffmpeg):
        result = session.record_start(path=str(tmp_path))
        assert result["status"] == "error"
        assert "not a directory" in result["error"]

    def test_start_requires_ffmpeg(self, session, monkeypatch):
        def missing(_name):
            raise ImportError("no imageio")

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", missing)
        result = session.record_start()
        assert result["status"] == "error"
        assert "ffmpeg" in result["error"]
        assert "[ffmpeg]" in result["error"]

    def test_start_twice_is_rejected(self, session, tmp_path, fake_ffmpeg):
        first = session.record_start(path=str(tmp_path / "one.mp4"))
        assert first["status"] == "ok"
        second = session.record_start(path=str(tmp_path / "two.mp4"))
        assert second["status"] == "error"
        assert "already in progress" in second["error"]
        assert session._recorder is not None
        assert session._recorder.path.name == "one.mp4"

    def test_start_failure_removes_callback(self, session, fake_ffmpeg):
        session._get_tab()
        tab = session._tab
        tab.command_error = RuntimeError("no screencast")
        tab.remove_callback_error = RuntimeError("no remove")
        result = session.record_start()
        assert result["status"] == "error"
        assert "no screencast" in result["error"]
        assert session._recorder is None
        assert len(tab.removed_callbacks) == 1

    def test_bad_frame_and_failed_stop_are_reported(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        tab = session._tab
        tab.command_error = RuntimeError("ack failed")
        callback = screencast_callback(tab)
        callback({"params": {"metadata": {"timestamp": 1.0}, "sessionId": 1}})
        callback(
            {
                "params": {
                    "data": "!!!!",
                    "metadata": {"timestamp": 2.0},
                    "sessionId": 1,
                }
            }
        )
        recorder = session._recorder
        assert recorder is not None
        assert recorder.frames == []
        screencast_id = screencast_callback_id(tab)
        result = session.record_stop()
        assert result["status"] == "error"
        assert "no video frames" in result["error"]
        assert tab.removed_callbacks == [screencast_id]

    def test_frame_ack_is_skipped_without_callback_context(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        recorder = session._recorder
        assert recorder is not None
        recorder.tab = None
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=1.0, session_id=1))
        callback(frame_event(timestamp=2.0, session_id=None))
        assert len(recorder.frames) == 2
        assert session.record_stop()["status"] == "ok"

    def test_frame_without_metadata_uses_wall_clock(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=None))
        assert len(session._recorder.frames) == 1
        assert session.record_stop()["status"] == "ok"

    def test_fps_caps_captured_frames(self, session, fake_ffmpeg):
        assert session.record_start(fps=1)["status"] == "ok"
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=1.0))
        callback(frame_event(timestamp=1.5))
        assert len(session._recorder.frames) == 1
        assert session.record_stop()["frames"] == 1

    def test_ffmpeg_failure_is_reported(self, session, tmp_path, fake_ffmpeg):
        state, _calls = fake_ffmpeg
        target = tmp_path / "out.mp4"
        assert session.record_start(path=str(target))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        state["returncode"] = 1
        state["stderr"] = "boom"
        state["write"] = False
        result = session.record_stop()
        assert result["status"] == "error"
        assert "boom" in result["error"]
        assert not target.exists()

    def test_ffmpeg_without_output_is_reported(self, session, tmp_path, fake_ffmpeg):
        state, _calls = fake_ffmpeg
        target = tmp_path / "out.mp4"
        assert session.record_start(path=str(target))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        state["write"] = False
        result = session.record_stop()
        assert result["status"] == "error"
        assert "did not write" in result["error"]
        assert not target.exists()

    def test_stop_without_recording(self, session):
        result = session.record_stop()
        assert result["status"] == "error"
        assert "no video recording" in result["error"]
        assert session.take_last_recording() is None

    def test_stop_then_close_does_not_repeat_recording(
        self, session, tmp_path, fake_ffmpeg
    ):
        assert session.record_start(path=str(tmp_path / "once.mp4"))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        assert session.record_stop()["status"] == "ok"
        assert session.close() is True
        assert session.last_recording is None
        assert session.take_last_recording() is None

    def test_close_encodes_active_recording(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        assert session.close() is True
        recording = session.last_recording
        assert recording is not None
        assert recording["status"] == "ok"
        result = session.take_last_recording()
        assert result == recording
        assert session.take_last_recording() is None
        assert session.close() is False

    def test_close_reports_recording_failure(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        session._tab = None
        assert session.close() is True
        assert session.last_recording is not None
        assert session.last_recording["status"] == "error"

    def test_reset_discards_recording(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        recorder = session._recorder
        assert recorder is not None
        frames_dir = recorder.frames_dir
        session._reset()
        assert session._recorder is None
        assert not frames_dir.exists()
        assert session.last_recording is not None
        assert "disconnected" in session.last_recording["error"]


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


class TestModuleFunctions:
    def test_module_functions_delegate_to_default_session(self, default_session):
        assert (
            browser_module.browser("navigate", value="https://example.com")["status"]
            == "ok"
        )
        default_session._tab.script_response = snapshot_response([])
        assert browser_module.browser_open("https://example.com")["action"] == "open"
        assert browser_module.browser_snapshot()["status"] == "ok"
        default_session._tab.script_response = {
            "result": {"result": {"type": "string", "value": "ok"}}
        }
        assert browser_module.browser_evaluate("return 'ok'")["result"] == "ok"
        assert browser_module.browser_screenshot(as_base64=True)["base64"] == "ZmFrZQ=="

    def test_module_recording_delegates_to_default_session(
        self, default_session, tmp_path, fake_ffmpeg
    ):
        result = browser_module.browser_record_start(path=str(tmp_path / "module.mp4"))
        assert result["status"] == "ok"
        screencast_callback(default_session._tab)(frame_event())
        stopped = browser_module.browser_record_stop()
        assert stopped["status"] == "ok"
        assert stopped["frames"] == 1

    def test_module_close_includes_recording(
        self, default_session, tmp_path, fake_ffmpeg
    ):
        assert (
            browser_module.browser_record_start(path=str(tmp_path / "close.mp4"))[
                "status"
            ]
            == "ok"
        )
        screencast_callback(default_session._tab)(frame_event())
        result = browser_module.browser_close()
        assert result["status"] == "ok"
        assert result["recording"]["status"] == "ok"
        assert Path(result["recording"]["path"]).exists()


class TestBrowserBundle:
    DEFAULT_NAMES: ClassVar[list[str]] = [
        "browser_open",
        "browser",
        "browser_snapshot",
        "browser_tabs",
        "browser_network",
        "browser_cookies",
        "browser_screenshot",
        "browser_evaluate",
        "browser_close",
    ]
    RECORDING_NAMES: ClassVar[list[str]] = [
        "browser_record_start",
        "browser_record_stop",
    ]

    def test_browser_returns_dspy_tools(self):
        tools = browser_module.Browser()
        assert isinstance(tools, browser_module.BrowserTools)
        assert [tool.name for tool in tools] == self.DEFAULT_NAMES
        assert all(isinstance(tool, dspy.Tool) for tool in tools)

    def test_session_options_build_an_isolated_session(self, tmp_path):
        tools = browser_module.Browser(
            user_data_dir=str(tmp_path / "profile"),
            dialog_policy="accept",
        )
        try:
            assert tools.session is not tools_module._default_session
            assert tools.session.user_data_dir == (tmp_path / "profile").resolve()
            assert tools.session.dialog_policy == "accept"
            assert [tool.name for tool in tools] == self.DEFAULT_NAMES
        finally:
            tools.close()

    def test_session_options_cover_the_rest(self):
        tools = browser_module.Browser(
            headless=False,
            timeout=5,
            allowed_schemes=("https",),
            url_policy=lambda url: True,
        )
        try:
            assert tools.session.headless is False
            assert tools.session.timeout == 5
            assert tools.session.allowed_schemes == frozenset({"https"})
            assert tools.session.url_policy is not None
        finally:
            tools.close()

    def test_session_and_options_conflict(self):
        with pytest.raises(ValueError, match="not both"):
            browser_module.Browser(session=BrowserSession(), user_data_dir="./profile")

    def test_no_options_uses_the_shared_session(self, default_session):
        assert browser_module.Browser().session is default_session

    def test_browser_requires_pydoll(self, monkeypatch):
        monkeypatch.setattr(_pydoll, "_loaded", False)
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pydoll" or name.startswith("pydoll."):
                raise ImportError("pydoll is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match=r"\[browser\]"):
            browser_module.Browser()

    def test_browser_can_include_recording(self):
        tools = browser_module.Browser(include_recording=True)
        assert [tool.name for tool in tools] == [
            *self.DEFAULT_NAMES[:-1],
            *self.RECORDING_NAMES,
            "browser_close",
        ]

    def test_browser_can_include_search(self):
        tools = browser_module.Browser(include_search=True)
        assert [tool.name for tool in tools] == [*self.DEFAULT_NAMES, "web_search"]

    def test_browser_binds_tools_to_session(self, session):
        tools = browser_module.Browser(session=session)
        assert tools.session is session
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        snapshot = next(tool for tool in tools if tool.name == "browser_snapshot")
        assert snapshot.func()["status"] == "ok"
        assert snapshot.args["format"]["default"] == "nodes"
        assert "include_text" not in snapshot.args

    def test_session_tools_binds_tools_to_session(self, session):
        tools = session.tools(include_recording=True)
        assert tools.session is session
        assert [tool.name for tool in tools] == [
            *self.DEFAULT_NAMES[:-1],
            *self.RECORDING_NAMES,
            "browser_close",
        ]

    def test_browser_close_delegates_to_session(self, session):
        calls = []
        session.close = lambda: calls.append(True) or True
        session.take_last_recording = lambda: None
        tools = browser_module.Browser(session=session)
        assert tools.close() == {"status": "ok", "action": "close", "closed": True}
        assert calls == [True]

    def test_browser_close_tool_delegates_to_session(self, session):
        calls = []
        session.close = lambda: calls.append(True) or False
        session.take_last_recording = lambda: None
        tools = browser_module.Browser(session=session)
        close_tool = next(tool for tool in tools if tool.name == "browser_close")
        assert close_tool.func() == {
            "status": "ok",
            "action": "close",
            "closed": False,
        }
        assert calls == [True]

    def test_browser_context_manager_closes_session(self, session):
        calls = []
        session.close = lambda: calls.append(True) or False
        session.take_last_recording = lambda: None
        with browser_module.Browser(session=session) as tools:
            assert isinstance(tools, browser_module.BrowserTools)
        assert calls == [True]

    def test_browser_requires_dspy(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "dspy":
                raise ImportError("missing dspy")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ImportError, match="requires DSPy"):
            browser_module.Browser()


class TestHelpers:
    def test_render_empty_nodes(self):
        assert _render_nodes([]) == ""

    def test_screenshot_format(self):
        assert screenshot._screenshot_format(None) == "png"
        assert screenshot._screenshot_format("shot.JPG") == "jpeg"
        assert screenshot._screenshot_format("shot.webp") == "webp"

    def test_ffmpeg_binary(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        assert coerce._ffmpeg_binary() == "/usr/bin/ffmpeg"

    def test_ffmpeg_binary_falls_back_to_imageio(self, monkeypatch):
        module = SimpleNamespace(get_ffmpeg_exe=lambda: "/bundle/ffmpeg")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", lambda _name: module)
        assert coerce._ffmpeg_binary() == "/bundle/ffmpeg"

    def test_ffmpeg_binary_reports_bundled_binary_error(self, monkeypatch):
        def unavailable():
            raise RuntimeError("no bundled binary")

        module = SimpleNamespace(get_ffmpeg_exe=unavailable)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", lambda _name: module)
        with pytest.raises(errors._ToolError, match="no bundled binary"):
            coerce._ffmpeg_binary()

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


def watch_callback(tab, event):
    for name, callback, _temporary in tab.callbacks.values():
        if name == event:
            return callback
    raise AssertionError(f"no {event} callback registered")


def fire_console(tab, *args, level="log"):
    watch_callback(tab, RuntimeEvent.CONSOLE_API_CALLED)(
        {"params": {"type": level, "args": list(args)}}
    )


def fire_error(tab, text="Uncaught TypeError: boom"):
    watch_callback(tab, RuntimeEvent.EXCEPTION_THROWN)(
        {"params": {"exceptionDetails": {"exception": {"description": text}}}}
    )


def fire_dialog(tab, message="Are you sure?", kind="confirm", prompt=None):
    params = {"type": kind, "message": message}
    if prompt is not None:
        params["defaultPrompt"] = prompt
    watch_callback(tab, PageEvent.JAVASCRIPT_DIALOG_OPENING)({"params": params})


class TestActionability:
    def test_click_refuses_hidden(self, session):
        session._get_tab()
        session._tab.element.visible = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "not visible" in result["error"]
        assert session._tab.element.click_calls == 0

    def test_click_refuses_disabled(self, session):
        session._get_tab()
        session._tab.element.enabled = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "disabled" in result["error"]
        assert session._tab.element.click_calls == 0

    def test_click_refuses_covered(self, session):
        session._get_tab()
        session._tab.element.on_top = False
        result = session.browser("click", target="#go")
        assert result["status"] == "error"
        assert "covered" in result["error"]
        assert session._tab.element.scroll_calls == 1
        assert session._tab.element.click_calls == 0

    def test_click_probe_errors_do_not_block(self, session):
        session._get_tab()

        def broken():
            raise RuntimeError("no probe")

        session._tab.element.is_visible = broken
        session._tab.element.is_on_top = None
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert session._tab.element.click_calls == 1

    def test_scroll_into_view_error_is_ignored(self, session):
        session._get_tab()

        def broken():
            raise RuntimeError("no scroll")

        session._tab.element.scroll_into_view = broken
        assert session.browser("click", target="#go")["status"] == "ok"

    def test_hover_skips_enabled_check(self, session):
        session._get_tab()
        session._tab.element.enabled = False
        result = session.browser("hover", target="#menu")
        assert result["status"] == "ok"
        assert session._tab.element.hover_calls == 1

    def test_type_refuses_covered_field(self, session):
        session._get_tab()
        session._tab.element.on_top = False
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "error"
        assert "covered" in result["error"]
        assert session._tab.element.typed == []

    def test_type_refuses_non_editable(self, session):
        session._get_tab()
        session._tab.element.editable = False
        result = session.browser("type", target="#note", value="hi")
        assert result["status"] == "error"
        assert "does not accept text" in result["error"]

    def test_probe_bool_accepts_properties(self):
        element = SimpleNamespace(is_enabled=True)
        assert dom._probe_bool(element, "is_enabled") is True
        assert dom._probe_bool(element, "is_visible") is True


class TestEffect:
    def test_click_reports_dom_change(self, session):
        session._get_tab()
        url = "https://example.com/page"
        session._tab.probe_responses = [
            {"url": url, "mutations": 0},
            {"url": url, "mutations": 4},
        ]
        result = session.browser("click", target="#go")
        assert result["changed"] is True
        assert result["effect"] == "dom"
        assert result["mutations"] == 4
        assert "warning" not in result

    def test_click_reports_navigation(self, session):
        session._get_tab()
        session._tab.probe_responses = [
            {"url": "https://example.com/a", "mutations": 0},
            {"url": "https://example.com/b", "mutations": 0},
        ]
        result = session.browser("click", target="#go")
        assert result["effect"] == "navigation"
        assert result["changed"] is True

    def test_click_warns_when_nothing_changed(self, session):
        result = session.browser("click", target="#go")
        assert result["changed"] is False
        assert result["effect"] == "none"
        assert "did not change" in result["warning"]

    def test_js_fallback_warning_is_reported(self, session):
        session._get_tab()
        session._tab.element.click_error = RuntimeError("blocked")
        result = session.browser("click", target="#go")
        assert result["method"] == "js"
        assert "JavaScript click" in result["warning"]

    def test_probe_failure_omits_effect(self, session):
        session._get_tab()
        session._tab.probe_error = RuntimeError("no probe")
        result = session.browser("click", target="#go")
        assert result["status"] == "ok"
        assert "changed" not in result

    def test_press_reports_effect(self, session):
        session._get_tab()
        url = "https://example.com/page"
        session._tab.probe_responses = [
            {"url": url, "mutations": 0},
            {"url": url, "mutations": 2},
        ]
        result = session.browser("press", value="Enter")
        assert result["effect"] == "dom"
        assert "warning" not in result

    def test_press_warns_when_nothing_changed(self, session):
        result = session.browser("press", value="Enter")
        assert result["changed"] is False
        assert "did not change" in result["warning"]


class TestTypeVerification:
    def test_verified(self, session):
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["verified"] is True

    def test_mismatch_reports_actual(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["value"] = "hi@example.co"
        result = session.browser("type", target="#email", value="hi@example.com")
        assert result["verified"] is False
        assert result["actual"] == "hi@example.co"
        assert result["actual_length"] == len("hi@example.co")
        assert "differs after typing" in result["warning"]

    def test_password_mismatch_is_redacted(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["type"] = "password"
        element.field_state["value"] = "masked"
        result = session.browser("type", target="#pw", value="secret")
        assert result["verified"] is False
        assert "actual" not in result

    def test_contenteditable_matches_on_text(self, session):
        session._get_tab()
        element = session._tab.element
        element.mirror_typing = False
        element.field_state["tag"] = "div"
        element.field_state["value"] = None
        element.field_state["text"] = "draft hello world"
        result = session.browser("type", target="#note", value="hello")
        assert result["verified"] is True

    def test_unreadable_field_skips_verification(self, session):
        session._get_tab()
        session._tab.element.field_state_error = RuntimeError("no state")
        result = session.browser("type", target="#email", value="hi")
        assert result["status"] == "ok"
        assert "verified" not in result


class TestSelectVerification:
    def test_verified(self, session):
        result = session.browser("select", target="#size", value="selected")
        assert result["verified"] is True

    def test_mismatch_warns(self, session):
        result = session.browser("select", target="#size", value="large")
        assert result["verified"] is False
        assert "after selecting" in result["warning"]

    def test_missing_return_value_is_not_verified(self, session):
        session._get_tab()
        session._tab.element.execute_script = lambda *_a, **_k: {
            "result": {"result": {"type": "undefined"}}
        }
        result = session.browser("select", target="#size", value="large")
        assert result["status"] == "ok"
        assert "verified" not in result


class TestWaitConditions:
    def test_wait_for_target_selector(self, session):
        result = session.browser("wait", target="#done")
        assert result["status"] == "ok"
        assert result["waited_for"] == "#done"
        script, timeout = session._tab.wait_script_calls[0]
        assert "document.querySelector" in script
        assert '"#done"' in script
        assert timeout == 15.0

    def test_wait_for_text(self, session):
        result = session.browser("wait", value="text=Done")
        assert result["waited_for"] == "text 'Done'"
        assert '"Done"' in session._tab.wait_script_calls[0][0]

    def test_wait_for_partial_text(self, session):
        result = session.browser("wait", value="text*=Don")
        assert result["waited_for"] == "text 'Don'"

    def test_wait_for_url_prefix(self, session):
        result = session.browser("wait", value="url=https://example.com/next")
        assert result["waited_for"].startswith("url starting with")
        assert "location.href.indexOf" in session._tab.wait_script_calls[0][0]

    def test_wait_for_bare_url(self, session):
        result = session.browser("wait", value="https://example.com/next*")
        assert result["status"] == "ok"
        assert '"https://example.com/next"' in session._tab.wait_script_calls[0][0]

    def test_wait_for_selector_prefix(self, session):
        result = session.browser("wait", value="selector=#done")
        assert result["waited_for"] == "#done"

    def test_wait_clamps_timeout(self, session):
        session.browser("wait", target="#done", timeout=120)
        assert session._tab.wait_script_calls[0][1] == 30.0

    def test_wait_timeout_is_reported(self, session):
        session._get_tab()
        session._tab.wait_script_error = TimeoutException("too slow")
        result = session.browser("wait", value="text=Done", timeout=5)
        assert result["status"] == "error"
        assert "timed out after 5s waiting for text 'Done'" in result["error"]

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("selector=", "selector= requires"),
            ("text=", "text= requires"),
            ("text*=", "text*= requires"),
            ("url=", "url= requires"),
        ],
    )
    def test_wait_rejects_empty_condition(self, session, value, message):
        result = session.browser("wait", value=value)
        assert result["status"] == "error"
        assert message in result["error"]


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


def fire_request(tab, request_id="1", url="https://example.com/api", method="GET"):
    watch_callback(tab, NetworkEvent.REQUEST_WILL_BE_SENT)(
        {
            "params": {
                "requestId": request_id,
                "type": "Fetch",
                "request": {"method": method, "url": url},
            }
        }
    )


def fire_response(tab, request_id="1", status=200, mime="application/json", **extra):
    response = {"status": status, "mimeType": mime, **extra}
    watch_callback(tab, NetworkEvent.RESPONSE_RECEIVED)(
        {"params": {"requestId": request_id, "response": response}}
    )


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


class TestNetwork:
    def test_requests_are_recorded(self, session):
        tab = session._get_tab()
        fire_request(tab)
        fire_response(tab)
        result = session.network()
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["requests"] == [
            {
                "id": "1",
                "method": "GET",
                "url": "https://example.com/api",
                "type": "Fetch",
                "status": 200,
                "mime": "application/json",
            }
        ]

    def test_cached_response_is_flagged(self, session):
        tab = session._get_tab()
        fire_request(tab)
        fire_response(tab, fromDiskCache=True)
        assert session.network()["requests"][0]["cached"] is True

    def test_failed_request_is_recorded(self, session):
        tab = session._get_tab()
        fire_request(tab, request_id="9", url="https://example.com/broken")
        watch_callback(tab, NetworkEvent.LOADING_FAILED)(
            {"params": {"requestId": "9", "errorText": "net::ERR_FAILED"}}
        )
        assert session.network()["requests"][0]["failed"] == "net::ERR_FAILED"

    def test_redirect_updates_the_same_entry(self, session):
        tab = session._get_tab()
        fire_request(tab, url="https://example.com/one")
        fire_request(tab, url="https://example.com/two")
        requests = session.network()["requests"]
        assert len(requests) == 1
        assert requests[0]["url"] == "https://example.com/two"

    def test_filter_by_url_and_id(self, session):
        tab = session._get_tab()
        fire_request(tab, request_id="req-1", url="https://example.com/alpha")
        fire_request(tab, request_id="req-2", url="https://example.com/beta")
        assert [r["id"] for r in session.network(value="beta")["requests"]] == ["req-2"]
        assert [r["id"] for r in session.network(value="req-1")["requests"]] == [
            "req-1"
        ]

    def test_limit_keeps_the_newest(self, session):
        tab = session._get_tab()
        for index in range(5):
            fire_request(tab, request_id=str(index), url=f"https://e.com/{index}")
        requests = session.network(limit=2)["requests"]
        assert [r["id"] for r in requests] == ["3", "4"]

    def test_entries_are_evicted(self, session):
        tab = session._get_tab()
        for index in range(config._MAX_NETWORK_ENTRIES + 5):
            fire_request(tab, request_id=str(index), url=f"https://e.com/{index}")
        requests = session.network(limit=1000)["requests"]
        assert len(requests) == config._MAX_NETWORK_ITEMS

    def test_clear(self, session):
        tab = session._get_tab()
        fire_request(tab)
        assert session.network("clear")["status"] == "ok"
        assert session.network()["requests"] == []

    def test_body(self, session):
        tab = session._get_tab()
        tab.network_body = "x" * 5000
        result = session.network("body", "1")
        assert result["truncated"] is True
        assert len(result["body"]) == config._MAX_NETWORK_BODY
        assert tab.requested_body == "1"

    def test_body_requires_an_id(self, session):
        result = session.network("body")
        assert result["status"] == "error"
        assert "request id" in result["error"]

    def test_unknown_action(self, session):
        result = session.network("har")
        assert result["status"] == "error"
        assert "requests, clear, or body" in result["error"]

    def test_bad_events_are_logged(self, session):
        session._get_tab()
        watch = session._watch
        watch.on_request(None)
        watch.on_response(None)
        watch.on_failed(None)
        assert session.network()["requests"] == []

    def test_request_without_an_id_is_ignored(self, session):
        tab = session._get_tab()
        watch_callback(tab, NetworkEvent.REQUEST_WILL_BE_SENT)(
            {"params": {"request": {"method": "GET", "url": "https://e.com"}}}
        )
        assert session.network()["requests"] == []

    def test_events_for_unknown_requests_are_ignored(self, session):
        tab = session._get_tab()
        fire_response(tab, request_id="missing")
        watch_callback(tab, NetworkEvent.LOADING_FAILED)(
            {"params": {"requestId": "missing", "errorText": "x"}}
        )
        assert session.network()["requests"] == []

    def test_limit_is_coerced(self):
        limit = tabs._coerce_limit
        assert limit(None) == 20
        assert limit("5") == 5
        assert limit(0) == 1
        assert limit(1000) == config._MAX_NETWORK_ITEMS
        with pytest.raises(errors._ToolError, match="limit must be"):
            limit("many")

    def test_module_tool_uses_default_session(self, default_session):
        tab = default_session._get_tab()
        fire_request(tab)
        assert browser_module.browser_network()["count"] == 1


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
