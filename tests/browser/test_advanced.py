import json
import threading

from dspy_agent_tools.browser import BrowserSession
from dspy_agent_tools.browser.watch import _PageWatch


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


def test_restore_validates_before_opening(tmp_path):
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


def test_pdf_validates_before_browser_start(tmp_path):
    session = BrowserSession()
    assert session.pdf(str(tmp_path / "nope.txt"))["status"] == "error"
    assert session._browser is None
