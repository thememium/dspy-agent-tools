"""Tests for retry handling in dspy_agent_tools.mcp."""

import asyncio
import json
import time
from types import SimpleNamespace

from dspy_agent_tools.mcp.retry import _HTTPFailures

from .helpers import start_retry


class TestReadOnlyRetry:
    def test_replays_read_only_tool_until_it_answers(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            assert json.loads(by_name["flaky_read"](text="hi")) == {"echo": "hi"}

    def test_async_path_replays_too(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            result = asyncio.run(by_name["flaky_read"].acall(text="hi"))
            assert json.loads(result) == {"echo": "hi"}

    def test_structured_mode_replays_mapping_payloads(self):
        with start_retry(retry_timeout=5, result_mode="structured") as tools:
            by_name = {tool.name: tool for tool in tools}
            result = by_name["flaky_read"](text="hi")
            payload = json.loads(result) if isinstance(result, str) else result
            assert payload == {"echo": "hi"}

    def test_write_tools_are_never_replayed(self):
        with start_retry(retry_timeout=5) as tools:
            by_name = {tool.name: tool for tool in tools}
            started = time.monotonic()
            result = by_name["flaky_write"](text="hi")
            assert time.monotonic() - started < 1.0
            assert json.loads(result)["error"] == "write refused"

    def test_zero_retry_timeout_disables_replay(self):
        with start_retry(retry_timeout=0) as tools:
            by_name = {tool.name: tool for tool in tools}
            result = by_name["flaky_read"](text="hi")
            assert json.loads(result)["error"] == "still starting"

    def test_retry_budget_bounds_a_persistent_error(self):
        with start_retry(retry_timeout=0.2) as tools:
            by_name = {tool.name: tool for tool in tools}
            started = time.monotonic()
            result = by_name["always_fails"](text="hi")
            elapsed = time.monotonic() - started
            assert 0.2 <= elapsed < 2.0
            assert json.loads(result)["error"] == "still unavailable"


class TestHttpFailures:
    def _response(self, status, url="http://example/mcp"):
        return SimpleNamespace(status_code=status, request=SimpleNamespace(url=url))

    def test_describes_auth_failures(self):
        failures = _HTTPFailures()
        asyncio.run(failures.record(self._response(401)))
        assert failures.describe() == (
            "MCP server answered HTTP 401 from http://example/mcp; pass "
            "headers={'Authorization': 'Bearer <token>'} or an auth= handler to MCP()"
        )

    def test_describes_not_found(self):
        failures = _HTTPFailures()
        asyncio.run(failures.record(self._response(404)))
        assert failures.describe() == (
            "MCP server answered HTTP 404 from http://example/mcp; "
            "the MCP endpoint was not found, check the URL"
        )

    def test_describes_other_statuses_without_hint(self):
        failures = _HTTPFailures()
        assert failures.describe() is None
        asyncio.run(failures.record(self._response(200)))
        assert failures.describe() is None
        asyncio.run(failures.record(self._response(500, url="")))
        assert failures.describe() == "MCP server answered HTTP 500"
