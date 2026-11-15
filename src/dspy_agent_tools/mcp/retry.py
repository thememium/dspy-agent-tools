"""Retry policy for read-only MCP tools and HTTP failure reporting."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any

from .adapter import _error_payload

_RETRY_INITIAL_DELAY = 0.5  # seconds
_RETRY_MAX_DELAY = 2.0  # seconds


def _retrying(func: Callable[..., Any], *, timeout: float) -> Callable[..., Any]:
    """Replay a read-only tool while it answers with an error payload.

    MCP servers commonly finish startup work (an index reconcile, a cache
    warm-up) after the handshake, advertising their tools immediately and
    answering read calls with an error until it ends. A side-effect-free tool
    can be replayed safely, so the agent sees the answer instead of a
    dead-end observation. The retry gives up at ``timeout`` and returns the
    last error payload unchanged; non-read-only tools are never wrapped.
    """
    if timeout <= 0:
        return func

    @functools.wraps(func)
    async def call(*args: Any, **kwargs: Any) -> Any:
        deadline = time.monotonic() + timeout
        delay = _RETRY_INITIAL_DELAY
        while True:
            result = await func(*args, **kwargs)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _error_payload(result):
                return result
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _RETRY_MAX_DELAY)

    return call


class _HTTPFailures:
    """Last failing HTTP response seen by an MCP HTTP client.

    The Streamable HTTP client reports every non-2xx response as the same
    generic ``INTERNAL_ERROR`` (``"Server returned an error response"``), so a
    rejected request is otherwise indistinguishable from a broken server.
    Recording the status here lets the startup error say what was rejected.
    """

    __slots__ = ("status", "url")

    def __init__(self) -> None:
        self.status: int | None = None
        self.url: str | None = None

    async def record(self, response: Any) -> None:
        if response.status_code >= 400:
            self.status = response.status_code
            self.url = str(response.request.url)

    def describe(self) -> str | None:
        if self.status is None:
            return None
        where = f" from {self.url}" if self.url else ""
        if self.status in (401, 403):
            hint = (
                "; pass headers={'Authorization': 'Bearer <token>'} or an "
                "auth= handler to MCP()"
            )
        elif self.status == 404:
            hint = "; the MCP endpoint was not found, check the URL"
        else:
            hint = ""
        return f"MCP server answered HTTP {self.status}{where}{hint}"


def _watch_http_failures(client: Any, failures: _HTTPFailures) -> None:
    """Attach ``failures`` as a response hook on an HTTP client, if possible."""
    hooks = getattr(client, "event_hooks", None)
    if isinstance(hooks, dict):
        hooks.setdefault("response", []).append(failures.record)
