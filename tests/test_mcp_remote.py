"""Tests for remote MCP transports."""

import asyncio
import gc
import socket
import subprocess
import sys
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client

from dspy_agent_tools import MCP

SERVER = Path(__file__).parent / "fixtures" / "remote_server.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"MCP test server exited early:\n{stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"MCP test server did not start on port {port}")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _start(transport: str) -> tuple[str, subprocess.Popen]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), transport, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_until_ready(port, proc)
    except BaseException:
        _stop(proc)
        raise
    path = "/mcp" if transport == "streamable-http" else "/sse"
    return f"http://127.0.0.1:{port}{path}", proc


@pytest.fixture(scope="module")
def http_url():
    url, proc = _start("streamable-http")
    try:
        yield url
    finally:
        _stop(proc)


@pytest.fixture(scope="module")
def sse_url():
    url, proc = _start("sse")
    try:
        yield url
    finally:
        _stop(proc)


def _call_add(tools) -> str:
    by_name = {tool.name: tool for tool in tools}
    return asyncio.run(by_name["add"].acall(a=2, b=3))


class TestStreamableHTTP:
    def test_lists_and_calls_tools(self, http_url):
        with MCP(url=http_url) as tools:
            assert {tool.name for tool in tools} == {"add", "echo"}
            assert _call_add(tools) == "5"

    def test_headers_and_name_prefix(self, http_url):
        with MCP(url=http_url, headers={"X-Test": "1"}, name_prefix="x_") as tools:
            assert {tool.name for tool in tools} == {"x_add", "x_echo"}

    def test_custom_http_client_factory(self, http_url):
        with MCP(url=http_url, http_client_factory=create_mcp_http_client) as tools:
            assert _call_add(tools) == "5"

    def test_closes_when_tools_are_collected(self, http_url):
        tools = MCP(url=http_url)
        connection = tools._connections[0]
        thread = connection._thread
        ref = weakref.ref(connection)

        del connection
        del tools
        gc.collect()

        assert ref() is None
        assert not thread.is_alive()


class TestSSE:
    def test_lists_and_calls_tools(self, sse_url):
        with MCP(url=sse_url, transport="sse") as tools:
            assert {tool.name for tool in tools} == {"add", "echo"}
            assert _call_add(tools) == "5"

    def test_custom_http_client_factory(self, sse_url):
        with MCP(
            url=sse_url, transport="sse", http_client_factory=create_mcp_http_client
        ) as tools:
            assert _call_add(tools) == "5"


class TestCustomTransport:
    def test_sse_via_transport(self, sse_url):
        tools = MCP(transport=lambda: sse_client(sse_url))
        try:
            assert {tool.name for tool in tools} == {"add", "echo"}
            assert _call_add(tools) == "5"
        finally:
            tools.close()


def _notes(exc: BaseException) -> list[str]:
    """Collect exception notes from an exception and any nested sub-exceptions."""
    notes = [str(note) for note in getattr(exc, "__notes__", ())]
    for sub in getattr(exc, "exceptions", ()) or ():
        notes.extend(_notes(sub))
    return notes


class TestHTTPRejection:
    @pytest.mark.parametrize(
        ("status", "hint"),
        [
            (401, "Authorization"),
            (404, "not found"),
            (500, None),
        ],
    )
    def test_non_2xx_status_is_reported(self, status, hint):
        class Rejected(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"rejected"
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Rejected)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(BaseException) as excinfo:
                MCP(
                    url=f"http://127.0.0.1:{server.server_port}/mcp",
                    startup_timeout=5.0,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        note = next(
            (n for n in _notes(excinfo.value) if f"HTTP {status}" in n),
            None,
        )
        assert note is not None
        if hint is not None:
            assert hint in note
