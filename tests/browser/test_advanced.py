"""Tests for response waiting, CDP, PDF, and tab state persistence."""

import base64
import json
import threading

import pytest

from dspy_agent_tools.browser import BrowserSession
from dspy_agent_tools.browser.watch import _PageWatch

from .fakes import fire_request, fire_response


def test_response_wait_retains_events_and_wakes():
    watch = _PageWatch()
    watch.on_request(
        {"params": {"requestId": "1", "request": {"url": "https://example.org/data"}}}
    )
    watch.on_response({"params": {"requestId": "1", "response": {"status": 201}}})
    assert watch.wait_response("/data", 0)["status"] == 201
    watch.clear_network()
    watch.on_request(
        {"params": {"requestId": "2", "request": {"url": "https://example.org/other"}}}
    )
    timer = threading.Timer(
        0.01,
        lambda: watch.on_failed({"params": {"requestId": "2", "errorText": "aborted"}}),
    )
    timer.start()
    try:
        assert watch.wait_response("/other", 1)["failed"] == "aborted"
    finally:
        timer.join()


class TestWaitResponse:
    def test_requires_a_url(self, session):
        result = session.wait_response("", 0)
        assert result["status"] == "error"
        assert "url substring or request id is required" in result["error"]

    def test_returns_a_retained_response(self, session):
        tab = session._get_tab()
        fire_request(tab, "1", "https://example.org/data")
        fire_response(tab, "1", 201)
        result = session.wait_response("/data", 0)
        assert result["status"] == "ok"
        assert result["action"] == "wait_response"
        assert result["response"]["url"] == "https://example.org/data"
        assert result["response"]["status"] == 201

    def test_reports_a_timeout(self, session):
        session._get_tab()
        result = session.wait_response("/never", 0)
        assert result["status"] == "error"
        assert "timed out waiting for response" in result["error"]


class TestCdp:
    def test_requires_a_domain_method(self, session):
        with pytest.raises(ValueError, match=r"CDP Domain\.method"):
            session.cdp("Page")

    def test_sends_the_command_to_the_active_page(self, session):
        tab = session._get_tab()
        tab.command_response = {"result": {"frameTree": {}}}
        assert session.cdp("Page.getFrameTree", {"depth": 1}) == {
            "result": {"frameTree": {}}
        }
        assert tab.commands == [{"method": "Page.getFrameTree", "params": {"depth": 1}}]


class TestPdf:
    def test_writes_the_printed_document(self, session, tmp_path):
        tab = session._get_tab()
        tab.command_response = {
            "result": {"data": base64.b64encode(b"%PDF-1.4").decode()}
        }
        path = tmp_path / "page.pdf"
        result = session.pdf(str(path), width=4, height=6)
        assert result == {
            "status": "ok",
            "action": "pdf",
            "path": str(path.resolve()),
            "bytes": 8,
        }
        assert path.read_bytes() == b"%PDF-1.4"
        command = tab.commands[0]
        assert command["method"] == "Page.printToPDF"
        assert command["params"]["paperWidth"] == 4
        assert command["params"]["paperHeight"] == 6

    def test_rejects_non_positive_paper(self, session, tmp_path):
        result = session.pdf(str(tmp_path / "page.pdf"), width=0)
        assert result["status"] == "error"
        assert "paper dimensions must be positive" in result["error"]

    def test_pdf_validates_before_browser_start(self, tmp_path):
        session = BrowserSession()
        assert session.pdf(str(tmp_path / "nope.txt"))["status"] == "error"
        assert session._browser is None


class TestTabState:
    def test_save_tabs_writes_urls_and_active_index(self, session, tmp_path):
        session._get_tab()
        session._browser.new_tab("https://example.org/second")
        path = tmp_path / "tabs.json"
        result = session.save_tabs(str(path))
        assert result == {
            "status": "ok",
            "action": "save_tabs",
            "path": str(path.resolve()),
            "count": 2,
        }
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "version": 1,
            "urls": ["https://example.com/page", "https://example.org/second"],
            "active": 0,
        }

    def test_save_tabs_reports_an_unreadable_url(self, session, tmp_path):
        session._get_tab()
        session._tab.current_url_error = RuntimeError("detached")
        result = session.save_tabs(str(tmp_path / "tabs.json"))
        assert result["status"] == "error"
        assert "could not read every tab URL" in result["error"]

    def test_restore_tabs_opens_every_saved_url(self, session, tmp_path):
        path = tmp_path / "tabs.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "urls": ["https://example.org/a", "https://example.org/b"],
                    "active": 1,
                }
            ),
            encoding="utf-8",
        )
        result = session.restore_tabs(str(path))
        assert result == {"status": "ok", "action": "restore_tabs", "count": 2}
        opened = session._browser.tabs[1:]
        assert [tab.navigations for tab in opened] == [
            [("https://example.org/a", 15)],
            [("https://example.org/b", 15)],
        ]
        assert session._tab is opened[1]

    def test_restore_rejects_invalid_state(self, session, tmp_path):
        path = tmp_path / "tabs.json"
        path.write_text(
            json.dumps({"version": 1, "urls": "nope", "active": 0}),
            encoding="utf-8",
        )
        result = session.restore_tabs(str(path))
        assert result["status"] == "error"
        assert "invalid tab state" in result["error"]

    def test_restore_validates_before_opening(self, tmp_path):
        path = tmp_path / "tabs.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "urls": ["https://example.org/", "file:///secret"],
                    "active": 0,
                }
            )
        )
        session = BrowserSession()
        result = session.restore_tabs(str(path))
        assert result["status"] == "error"
        assert session._browser is None

    def test_restore_reports_a_browser_that_never_started(
        self, session, tmp_path, monkeypatch
    ):
        path = tmp_path / "tabs.json"
        path.write_text(
            json.dumps({"version": 1, "urls": ["https://example.org/"], "active": 0}),
            encoding="utf-8",
        )
        monkeypatch.setattr(session, "_get_tab", lambda: None)
        result = session.restore_tabs(str(path))
        assert result["status"] == "error"
        assert "browser failed to start" in result["error"]
