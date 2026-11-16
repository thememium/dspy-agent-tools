"""Tests for browser tool functions and the Browser bundle."""

import builtins
from pathlib import Path
from typing import ClassVar

import dspy
import pytest

from dspy_agent_tools.browser import BrowserSession, _pydoll
from dspy_agent_tools.browser import tools as tools_module

from .fakes import browser_module, frame_event, screencast_callback, snapshot_response


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
        "browser_wait_response",
        "browser_pdf",
        "browser_save_tabs",
        "browser_restore_tabs",
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

    def test_no_options_create_isolated_sessions(self, default_session):
        first, second = browser_module.Browser(), browser_module.Browser()
        assert first.session is not second.session
        assert first.session is not default_session

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


def test_humanize_and_stealth_options():
    first = browser_module.Browser(humanize=True)
    second = browser_module.Browser(stealth=True, fingerprint={"locale": "en-US"})
    assert first.session.humanize is True
    assert second.session.humanize is True
    assert second.session.fingerprint == {"locale": "en-US"}
    with pytest.raises(ValueError, match="not both"):
        browser_module.Browser(session=BrowserSession(), humanize=True)


def test_humanized_actions_use_pydoll_flags(session, monkeypatch):
    session.humanize = True
    tab = session._get_tab()
    calls = []
    monkeypatch.setattr(
        tab.element, "click", lambda **kwargs: calls.append(("click", kwargs))
    )
    monkeypatch.setattr(
        tab.element, "type_text", lambda value, **kwargs: calls.append(("type", kwargs))
    )
    monkeypatch.setattr(
        tab.element, "hover", lambda **kwargs: calls.append(("hover", kwargs))
    )
    monkeypatch.setattr(
        tab.scroll, "by", lambda *args, **kwargs: calls.append(("scroll", kwargs))
    )
    for action, value in (
        ("click", None),
        ("type", "test"),
        ("hover", None),
        ("scroll", "down"),
    ):
        assert session.browser(action, target="#target", value=value)["status"] == "ok"
    assert [name for name, _ in calls] == ["click", "type", "hover", "scroll"]
    assert all(kwargs["humanize"] is True for _, kwargs in calls)
