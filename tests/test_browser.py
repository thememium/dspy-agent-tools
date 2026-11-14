"""Tests for dspy_agent_tools.browser with a fake pydoll browser."""

import importlib
from pathlib import Path
from typing import ClassVar

import pytest
from pydoll.constants import ScrollPosition
from pydoll.exceptions import ConnectionException, FailedToStartBrowser
from pydoll.sync import Key

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

    def test_atexit_registered_after_start(self, session, monkeypatch):
        registered = []
        monkeypatch.setattr(browser_module.atexit, "register", registered.append)
        session.browser("navigate", value="https://example.com")
        assert len(registered) == 1
        assert registered[0].__self__ is session

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


class TestModuleFunctions:
    def test_module_functions_delegate_to_default_session(self, default_session):
        assert (
            browser_module.browser("navigate", value="https://example.com")["status"]
            == "ok"
        )
        default_session._tab.script_response = snapshot_response([])
        assert browser_module.browser_snapshot()["status"] == "ok"
        default_session._tab.script_response = {
            "result": {"result": {"type": "string", "value": "ok"}}
        }
        assert browser_module.browser_evaluate("return 'ok'")["result"] == "ok"
        assert browser_module.browser_screenshot(as_base64=True)["base64"] == "ZmFrZQ=="


class TestHelpers:
    def test_render_empty_nodes(self):
        assert _render_nodes([]) == ""

    def test_screenshot_format(self):
        assert browser_module._screenshot_format(None) == "png"
        assert browser_module._screenshot_format("shot.JPG") == "jpeg"
        assert browser_module._screenshot_format("shot.webp") == "webp"

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
