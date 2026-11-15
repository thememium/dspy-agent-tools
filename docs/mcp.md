# MCP servers

`dspy_agent_tools.mcp` connects to an MCP server, converts every tool it
advertises into a `dspy.Tool`, and returns them as an `MCPTools` list. The
session stays live on a background event loop while the tools or the list are
referenced, so repeated calls reuse one connection.

## Install

```bash
uv add "dspy-agent-tools[mcp]"
# or: pip install "dspy-agent-tools[mcp]"
```

The extra installs DSPy, `httpx2`, and MCP SDK 2.x. Importing `MCP` works
without it; calling `MCP()` raises `ImportError` with the install hint.

## Quick start

A local stdio server:

```python
import dspy
from dspy_agent_tools import MCP, bash_exec

files = MCP(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
)

agent = dspy.ReAct(
    "question -> answer",
    tools=files + [bash_exec],  # mix MCP tools with regular ones
)

result = agent(question="List the files inside /tmp")
print(result.answer)
```

A remote server:

```python
tools = MCP(
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer <token>"},
)
```

A custom async transport:

```python
def transport_factory():
    # Return an async context manager yielding (read, write).
    ...

tools = MCP(transport=transport_factory)
```

`MCP()` starts the server and lists its tools immediately. It does not wait
for the first tool call.

## Transports

| Call | Transport | Use it for |
| --- | --- | --- |
| `MCP(command=..., args=[...])` | stdio | A local MCP server process such as `npx`. |
| `MCP(url=...)` | Streamable HTTP | Hosted MCP servers; the MCP default. |
| `MCP(url=..., transport="sse")` | legacy HTTP+SSE | Older remote servers. |
| `MCP(transport=factory)` | custom | Any async transport yielding a read/write pair. |

`transport="auto"` is the default and infers the transport from `command=` vs
`url=`. The explicit string forms are `"stdio"`, `"http"`, and `"sse"`.

Validation is strict:

- Pass either `command=` or `url=`, not both.
- A transport factory cannot be combined with `command=` or `url=`.
- `transport="stdio"` requires `command=` and rejects `url=`.
- `transport="http"` and `transport="sse"` require `url=` and reject
  `command=`.

## Options

### All transports

| Option | Default | Effect |
| --- | --- | --- |
| `command` | `None` | Executable for a local stdio server. |
| `args` | `()` | Arguments passed to the stdio command. |
| `env` | `None` | Environment for the stdio server; `None` inherits the current environment. |
| `cwd` | `None` | Working directory for the stdio server. |
| `url` | `None` | Remote MCP endpoint. |
| `transport` | `"auto"` | `"auto"`, `"stdio"`, `"http"`, `"sse"`, or a custom factory. |
| `startup_timeout` | `60` | Seconds to wait for the server to initialize before the initial tool listing. |
| `name_prefix` | `""` | Prepended to every tool name. |
| `result_mode` | `"text"` | `"text"` flattens MCP text content to strings; `"structured"` returns the server's `structuredContent` when it provides it. |
| `retry_timeout` | `30` | Seconds to replay a read-only tool while it answers with an error payload; `0` disables the replay. |

### Remote transports

`url=`, `transport="http"`, and `transport="sse"` additionally accept:

| Option | Default | Effect |
| --- | --- | --- |
| `headers` | `None` | Extra HTTP headers, such as an `Authorization` bearer token. |
| `timeout` | `30` HTTP / `5` SSE | HTTP timeout in seconds. |
| `sse_read_timeout` | `300` | How long to wait on the server stream. |
| `auth` | `None` | An `httpx`/`httpx2` auth handler, such as an MCP OAuth client provider. |
| `http_client_factory` | `None` | Zero-argument callable returning an async HTTP client. |
| `terminate_on_close` | `True` | Ask the server to terminate the HTTP session on close (Streamable HTTP only). |

`http_client_factory` is created and closed on the connection's event loop, so
it is safe for clients with loop affinity. For Streamable HTTP it replaces the
default client, which means `headers`, `timeout`, and `auth` are ignored. For
legacy SSE it is passed to the SDK as `httpx_client_factory` alongside the
other options.

For legacy SSE, `headers`, `timeout`, `sse_read_timeout`, and `auth` are only
forwarded when the installed MCP SDK's `sse_client` accepts them.

## Tools and result modes

Every advertised tool becomes its own `dspy.Tool`. The tool's real signature
is rebuilt from its MCP input schema, so `dspy.RLM` sees the actual parameter
names instead of a generic `(*args, **kwargs)` wrapper.

`result_mode` controls the value the tool returns:

- `"text"` (default) flattens MCP text content to a string or list of strings.
- `"structured"` returns the server's `structuredContent` when it provides it.

`name_prefix` is prepended to every tool name. Use it when two servers expose
identically named tools:

```python
files = MCP(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
)
github = MCP(url="https://api.githubcopilot.com/mcp/", name_prefix="gh_")
```

`MCP` returns every tool eagerly. For progressive discovery, wrap the result
in `Toolbox`:

```python
from dspy_agent_tools import MCP, Toolbox

catalog = Toolbox(MCP(url="https://example.com/mcp"), mode="search")
```

`MCPTools.entries` keeps the exact `CatalogEntry` metadata, including the
server's JSON input schema, for `Toolbox`.

## Startup retries

MCP servers often finish startup work after the handshake — an index
reconcile, a cache warm-up — advertising their tools immediately and answering
calls with an error payload (`{"error": ...}` or `{"ok": false}`) until it
ends.

A tool annotated `readOnlyHint` is replayed while it answers with such a
payload. `retry_timeout` (default 30 s) bounds the replay and `0` disables it.
The retry backs off from 0.5 s to 2 s. The last payload is returned unchanged
when the budget runs out.

Tools without the annotation are never replayed, so a side effect cannot be
duplicated.

## Lifecycle and cleanup

`MCP()` starts the server immediately on a background event-loop thread. The
session keeps running while the returned tools or the `MCPTools` list are
referenced.

No cleanup is required:

- When neither the list nor any of its tools is referenced, the connection is
  garbage-collected and the transport closes.
- An interpreter-exit fallback closes any remaining connections.
- Call `tools.close()` only to stop the server earlier. It is safe to call
  repeatedly.
- `MCPTools` is also a context manager; the sync and async forms both close
  the server on exit.

```python
with MCP(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
) as tools:
    agent = dspy.ReAct("question -> answer", tools=tools)
    result = agent(question="List the files inside /tmp")
# server stopped
```

`MCPTools` is a `list` subclass:

- Passing it as `tools=`, concatenating with `+` on either side, and unpacking
  all work.
- Addition keeps the MCP connections attached.
- `tools.close()` stops every server owned by that list.

Call `MCP` once per server and concatenate the results for several servers.
Use `name_prefix` when servers expose identically named tools.

## Sync and async

Both DSPy call paths work without extra configuration:

- `agent(...)` synchronously converts the async MCP calls, scoped to those
  tools. No global DSPy setting is changed.
- `await agent.acall(...)` runs fully asynchronously in async applications.

MCP tool calls are forwarded to the server's own event loop, so they work from
any caller loop.

## Errors

- Calling `MCP()` without the `[mcp]` extra raises `ImportError` with the
  install hint.
- `MCP()` raises `ValueError` for invalid transport combinations and
  `TimeoutError` when the server does not initialize within
  `startup_timeout`.
- Remote HTTP failures report the server's HTTP status and a short hint in the
  raised error.

## Safety

- A stdio MCP server is an arbitrary program launched with the caller's
  permissions. A remote server receives the arguments and data the tools send
  it. Treat both as trusted code or isolate them.
- `readOnlyHint` is trusted for retry decisions. It is an annotation from the
  server, not a security boundary.
- Use `name_prefix` when combining servers, and keep `Toolbox` ownership in
  mind: closing a catalog closes the MCP servers it owns.
