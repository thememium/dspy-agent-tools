"""Tests for serializable browser session state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dspy_agent_tools.browser import BrowserSession


def _storage_response(state: dict[str, dict[str, str]]) -> dict:
    return {"result": {"result": {"value": json.dumps(state)}}}


def test_capture_save_and_restore_state(
    session: BrowserSession, fake_chrome, tmp_path: Path
):
    session.browser("navigate", value="https://example.com")
    tab = fake_chrome.instances[0].tab
    tab.script_response = _storage_response(
        {"local": {"token": "abc"}, "session": {"step": "2"}}
    )

    state = session.capture_state()
    assert state["version"] == 1
    assert state["tabs"][0]["url"] == "https://example.com"
    assert state["tabs"][0]["storage"]["local"] == {"token": "abc"}
    assert state["cookies"][0]["name"] == "sid"

    path = tmp_path / "state.json"
    assert session.save_state(path)["status"] == "ok"
    assert json.loads(path.read_text())["tabs"][0]["storage"]["session"] == {
        "step": "2"
    }

    restored = session.restore_state(path)
    assert restored["count"] == 1
    reopened = fake_chrome.instances[0].tabs[-1]
    assert reopened.navigations[0][0] == "https://example.com"
    assert reopened.set_cookie_calls


def test_state_requires_a_path_and_rejects_invalid_data(
    session: BrowserSession, tmp_path: Path
):
    with pytest.raises(ValueError, match="needs a path"):
        session.save_state()
    with pytest.raises(ValueError, match="needs a path"):
        session.restore_state()

    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 2, "tabs": []}))
    with pytest.raises(ValueError, match="invalid browser state"):
        session.restore_state(path)

    path.write_text(json.dumps({"version": 1, "tabs": [{}]}))
    with pytest.raises(TypeError, match="invalid browser tab state"):
        session.restore_state(path)


def test_missing_browser_after_start_is_an_error(
    session: BrowserSession, monkeypatch, tmp_path: Path
):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"version": 1, "tabs": [{"url": "https://example.com", "active": True}]}
        )
    )
    session.state_path = path
    monkeypatch.setattr(session, "_get_tab", lambda: None)
    with pytest.raises(RuntimeError, match="failed to start"):
        session.restore_state()


def test_storage_capture_failure_is_empty(session: BrowserSession, fake_chrome):
    session.browser("navigate", value="https://example.com")
    fake_chrome.instances[0].tab.script_error = RuntimeError("no storage")
    assert session.capture_state()["tabs"][0]["storage"] == {}


def test_run_autosaves_and_recover_prefers_state(
    session: BrowserSession, fake_chrome, monkeypatch, tmp_path: Path
):
    path = tmp_path / "state.json"
    session.state_path = path
    session.browser("navigate", value="https://example.com")
    assert path.exists()

    called: list[str] = []
    monkeypatch.setattr(
        session,
        "restore_state",
        lambda: called.append("state") or {"status": "ok"},
    )
    assert session.recover()["status"] == "ok"
    assert called == ["state"]


def test_recover_falls_back_to_tabs_then_start(
    session: BrowserSession, monkeypatch, tmp_path: Path
):
    tabs = tmp_path / "tabs.json"
    tabs.write_text(
        json.dumps({"version": 1, "urls": ["https://example.com"], "active": 0})
    )
    session.tabs_state = tabs
    monkeypatch.setattr(
        session, "restore_tabs", lambda path: {"status": "ok", "action": "tabs"}
    )
    assert session.recover()["action"] == "tabs"

    session.tabs_state = None
    started: list[int] = []
    monkeypatch.setattr(session, "_get_tab", lambda: started.append(1))
    assert session.recover()["browser"] is True
    assert started == [1]


def test_close_saves_state_best_effort(session: BrowserSession, monkeypatch):
    session.state_path = Path("/tmp/unused-state.json")
    session.tabs_state = Path("/tmp/unused-tabs.json")
    session._tab = object()
    saved: list[str] = []

    def fail() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(session, "save_state", fail)
    monkeypatch.setattr(session, "save_tabs", lambda path: saved.append(path))
    assert session.close() is False
    assert saved == ["/tmp/unused-tabs.json"]


def test_run_save_failure_is_best_effort(
    session: BrowserSession, monkeypatch, tmp_path: Path
):
    session.state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        session,
        "save_state",
        lambda: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    result = session.browser("navigate", value="https://example.com")
    assert result["status"] == "ok"
