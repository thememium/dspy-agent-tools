"""Tests for browser network request tracking."""

import pytest
from pydoll.protocol.network.events import NetworkEvent

from dspy_agent_tools.browser import config, errors, tabs

from .fakes import (
    browser_module,
    fire_console,
    fire_request,
    fire_response,
    watch_callback,
)


class TestNetwork:
    def test_body_includes_pending_diagnostics(self, session):
        tab = session._get_tab()
        fire_console(tab, "network message")
        assert session.network("body", "1")["console"][0]["text"] == "network message"
        assert "console" not in session.network()

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
