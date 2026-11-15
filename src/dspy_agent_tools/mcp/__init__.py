"""Run MCP servers and expose their tools as ``dspy.Tool`` lists.

An MCP session is asynchronous and must stay open for as long as its tools
are used. The factories here hide that lifecycle: they connect on a background
event loop, convert every advertised tool with ``dspy.Tool.from_mcp_tool``,
and return the tools as an :class:`MCPTools` list that owns the live session.

Transports are chosen by :func:`MCP`:

- ``command=`` launches a local stdio server process (``npx``, a script, ...).
- ``url=`` connects to a remote Streamable HTTP endpoint (the MCP default).
- ``transport="sse"`` selects the legacy HTTP+SSE transport for an ``url``.
- ``transport=factory`` uses any custom async transport yielding a read/write
  pair (WebSocket, in-memory, ...).

``MCP`` returns every advertised tool eagerly. To keep definitions out of the
model context, wrap the result in :class:`dspy_agent_tools.Toolbox` with
``mode="search"`` or ``mode="code"``; ``Toolbox`` also accepts regular
``dspy.Tool`` objects and plain Python functions.

A tool the server annotates as read-only (``readOnlyHint``) is replayed while
it answers with an error payload (``{"error": ...}`` or ``{"ok": false}``),
which is how servers commonly report that startup work — an index reconcile,
a cache warm-up — is still running. ``retry_timeout`` (default 30 s, ``0``
disables) bounds the replay; other tools are never retried, so side effects
cannot be duplicated.

The returned tools work from both DSPy paths without extra configuration:
``agent(question=...)`` uses DSPy's async-to-sync conversion for the MCP call
only (the global setting is untouched), and ``await agent.acall(...)`` runs
fully asynchronously.

Sessions clean themselves up: the server stays alive while the returned tools
or list are referenced, then stops when they are garbage-collected, with an
interpreter-exit fallback. ``MCPTools.close()`` is only needed to release the
server earlier.

Requires the optional ``mcp`` extra (MCP SDK 2.x)::

    uv add "dspy-agent-tools[mcp]"
"""

from .tools import MCP, MCPTools

__all__ = ["MCP", "MCPTools"]
