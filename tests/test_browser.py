"""Tests for dspy_agent_tools.browser with a fake pydoll browser."""

import base64
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from pydoll.constants import ScrollPosition
from pydoll.exceptions import ConnectionException, FailedToStartBrowser
from pydoll.sync import Key, PageEvent

from dspy_agent_tools.browser import (
    BrowserSession,
    _render_nodes,
    _xpath_literal,
)

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

    def focus(self):
        self.focus_calls += 1

    def insert_text(self, value):
        if self.insert_error:
            raise self.insert_error
        self.inserted.append(value)

    def hover(self):
        self.hover_calls += 1

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
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
        self.page_events = 0
        self.callbacks = {}
        self.removed_callbacks = []
        self.commands = []
        self.command_error = None
        self.remove_callback_error = None
        self._next_callback_id = 0

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

    def query(self, expression, timeout=0, find_all=False, raise_exc=True):
        self.queries.append((expression, timeout, find_all, raise_exc))
        if self.query_error:
            raise self.query_error
        return self.query_result

    def execute_script(self, script, **kwargs):
        self.scripts.append((script, kwargs))
        if self.script_error:
            raise self.script_error
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

    def __init__(self, options=None, connection_port=None):
        self.options = options
        self.started = False
        self.stopped = False
        self.tab = FakeTab()
        FakeChrome.instances.append(self)

    def start(self):
        if FakeChrome.start_error:
            raise FakeChrome.start_error
        self.started = True
        return self.tab

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_chrome(monkeypatch):
    FakeChrome.instances = []
    FakeChrome.start_error = None
    monkeypatch.setattr(browser_module, "Chrome", FakeChrome)
    return FakeChrome


@pytest.fixture
def session(fake_chrome):
    session = BrowserSession()
    yield session
    BrowserSession.close(session)


@pytest.fixture
def default_session(monkeypatch, fake_chrome):
    session = BrowserSession()
    monkeypatch.setattr(browser_module, "_default_session", session)
    yield session
    BrowserSession.close(session)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(browser_module.time, "sleep", lambda _seconds: None)


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
        return browser_module.subprocess.CompletedProcess(
            command, returncode, stdout, stderr
        )

    monkeypatch.setattr(browser_module, "_ffmpeg_binary", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(browser_module.subprocess, "run", run)
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
        monkeypatch.setattr(browser_module, "_register_exit_hook", registered.append)
        session.browser("navigate", value="https://example.com")
        assert len(registered) == 1
        assert registered[0].__self__ is session

    def test_exit_hook_registered_once_across_restarts(self, session, monkeypatch):
        registered = []
        monkeypatch.setattr(browser_module, "_register_exit_hook", registered.append)
        session.browser("navigate", value="https://example.com")
        session.close()
        session.browser("navigate", value="https://example.com")
        assert len(registered) == 1

    def test_exit_hook_uses_threading_queue(self, monkeypatch):
        registered = []

        def callback():
            return None

        monkeypatch.setattr(
            browser_module.threading, "_register_atexit", registered.append
        )
        browser_module._register_exit_hook(callback)
        assert registered == [callback]

    def test_exit_hook_falls_back_to_atexit(self, monkeypatch):
        monkeypatch.delattr(browser_module.threading, "_register_atexit")
        registered = []

        def callback():
            return None

        monkeypatch.setattr(browser_module.atexit, "register", registered.append)
        browser_module._register_exit_hook(callback)
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
            ("file:///tmp/a", "file:///tmp/a"),
            ("about:blank", "about:blank"),
        ],
    )
    def test_url_normalization(self, session, value, expected):
        session.browser("navigate", value=value)
        assert session._tab.navigations[-1][0] == expected

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
    def test_history(self, session, action, no_sleep):
        result = session.browser(action)
        assert result["status"] == "ok"
        assert session._tab.scripts[-1][0] == f"history.{action}()"

    def test_reload(self, session):
        result = session.browser("reload")
        assert result["status"] == "ok"
        assert session._tab.refresh_calls == 1

    def test_wait(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(browser_module.time, "sleep", slept.append)
        result = session.browser("wait", value="2.5")
        assert result["status"] == "ok"
        assert result["seconds"] == 2.5
        assert slept == [2.5]

    def test_wait_defaults(self, session, monkeypatch):
        slept = []
        monkeypatch.setattr(browser_module.time, "sleep", slept.append)
        result = session.browser("wait")
        assert result["seconds"] == 1.0
        assert slept == [1.0]

    def test_wait_rejects_negative(self, session):
        result = session.browser("wait", value="-1")
        assert result["status"] == "error"
        assert "cannot be negative" in result["error"]

    def test_wait_rejects_text(self, session):
        result = session.browser("wait", value="soon")
        assert result["status"] == "error"
        assert "number of seconds" in result["error"]

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
        result = session.snapshot()
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

    def test_include_text_false(self, session):
        session._get_tab()
        session._tab.script_response = snapshot_response([])
        result = session.snapshot(include_text=False)
        assert "text" not in result
        assert result["node_count"] == 0

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
        monkeypatch.setattr(
            browser_module.tempfile, "gettempdir", lambda: str(tmp_path)
        )
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
        assert tab.page_events == 1
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

        stopped = session.record_stop()
        assert stopped["status"] == "ok"
        assert stopped["frames"] == 2
        assert stopped["duration_s"] == 0.6
        assert stopped["format"] == "mp4"
        assert stopped["bytes"] == len(b"fake-video")
        assert Path(stopped["path"]).read_bytes() == b"fake-video"
        assert tab.removed_callbacks == [1]
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
        assert browser_module._video_path(str(tmp_path / "clip")).name == "clip.mp4"

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

        monkeypatch.setattr(browser_module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(browser_module.importlib, "import_module", missing)
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
        assert tab.removed_callbacks == [1]

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
        result = session.record_stop()
        assert result["status"] == "error"
        assert "no video frames" in result["error"]
        assert tab.removed_callbacks == [1]

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
        assert session._tab.navigations == [("https://example.com", 15)]

    def test_navigation_error_skips_snapshot(self, session):
        result = session.open("   ")
        assert result["status"] == "error"
        assert result["action"] == "open"
        assert "navigate requires" in result["error"]
        assert session._tab.scripts == []

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


class TestHelpers:
    def test_render_empty_nodes(self):
        assert _render_nodes([]) == ""

    def test_screenshot_format(self):
        assert browser_module._screenshot_format(None) == "png"
        assert browser_module._screenshot_format("shot.JPG") == "jpeg"
        assert browser_module._screenshot_format("shot.webp") == "webp"

    def test_ffmpeg_binary(self, monkeypatch):
        monkeypatch.setattr(
            browser_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg"
        )
        assert browser_module._ffmpeg_binary() == "/usr/bin/ffmpeg"

    def test_ffmpeg_binary_falls_back_to_imageio(self, monkeypatch):
        module = SimpleNamespace(get_ffmpeg_exe=lambda: "/bundle/ffmpeg")
        monkeypatch.setattr(browser_module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            browser_module.importlib, "import_module", lambda _name: module
        )
        assert browser_module._ffmpeg_binary() == "/bundle/ffmpeg"

    def test_ffmpeg_binary_reports_bundled_binary_error(self, monkeypatch):
        def unavailable():
            raise RuntimeError("no bundled binary")

        module = SimpleNamespace(get_ffmpeg_exe=unavailable)
        monkeypatch.setattr(browser_module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            browser_module.importlib, "import_module", lambda _name: module
        )
        with pytest.raises(browser_module._ToolError, match="no bundled binary"):
            browser_module._ffmpeg_binary()

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
