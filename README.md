<a name="readme-top"></a>

<div align="center">
  <h3 align="center">DSPy Agent Tools</h3>

  <p align="center">
    A ready-made toolbox of Python functions for DSPy agents.
    <br />
    <a href="#table-of-contents"><strong>Explore the Documentation »</strong></a>
    <br />
    <a href="https://github.com/thememium/dspy-agent-tools/issues">Report Bug</a>
    ·
    <a href="https://github.com/thememium/dspy-agent-tools/issues">Request Feature</a>
  </p>
</div>

<!-- TABLE OF CONTENTS -->

<a name="table-of-contents"></a>

<details>
  <summary>Table of Contents</summary>
  <ol>
    <li><a href="#about">About</a></li>
    <li><a href="#quick-start">Quick Start</a></li>
    <li><a href="#usage">Usage</a></li>
    <li><a href="#tool-reference">Tool Reference</a></li>
    <li><a href="#development">Development</a></li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#license">License</a></li>
  </ol>
</details>

<!-- ABOUT -->

## About

DSPy Agent Tools is a library of **tool functions** for
[DSPy](https://dspy.ai) agents. Every tool is a plain, type-hinted Python
function with a tool-optimized docstring, so you can hand them straight to
`dspy.ReAct` or `dspy.RLM` — no adapters, no wrappers.

Three built-in tool families, plus a generic Toolbox and optional MCP bridge:

- **Bash** — shell execution, file search (ripgrep/fd-style, no binaries needed), directory listing, and URL fetching
- **Files** — read, write, and edit files with snapshot-based optimistic concurrency and tree-sitter block addressing
- **Search** — multi-engine web and news search with failover and explicit outcomes
- **MCP** (optional) — `MCP()` connects to a local **stdio** or remote **Streamable HTTP** / **HTTP+SSE** MCP server and returns an `MCPTools` list of `dspy.Tool` objects you can mix with the built-ins; `transport=<factory>` takes any custom async transport. Requires the `[mcp]` extra.
- **Toolbox** — `Toolbox([...], mode="search"|"code")` applies the same progressive-discovery facade to any mix of MCP servers, `dspy.Tool` objects, and plain Python functions.

Tools are just Python functions, exactly as
[DSPy's docs describe](https://dspy.ai/current/getting-started/react-and-tools/#tools-are-just-python-functions):
DSPy reads the name, type-hinted parameters, and docstring of each function to
assemble the instructions it sends to the LM. These docstrings are written for
that purpose — each one tells the model WHAT the tool does, WHEN to use it
instead of a sibling tool, and HOW to pass arguments.

Requires **Python 3.13+**; run agents with **DSPy 3.1+** (the `[mcp]` extra
installs it).

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- QUICK START -->

## Quick Start

### Install

Install dspy-agent-tools with uv (recommended):

```bash
uv add dspy-agent-tools
```

Or with pip:

```bash
pip install dspy-agent-tools
```

`bash_curl`, `bash_exec`, and `bash_ls` shell out to real `bash`/`curl`/`ls`
binaries, which must be on your PATH. The search tools
(`bash_ripgrep`, `bash_find`, `bash_glob`) run through the `rgapi` library —
ripgrep's and fd's engines as Python modules — so no `rg`/`fd` binary is
needed.

The MCP bridge is an optional extra (it pulls in DSPy, `httpx2`, and MCP
SDK 2.x). Importing `MCP` works without it; calling `MCP()` raises an
`ImportError` with the install hint:

```bash
uv add "dspy-agent-tools[mcp]"
```

### Basic Usage with dspy.ReAct

Import the tools you need and put them in the `tools` list:

```python
import dspy
from dspy_agent_tools import web_search, bash_fetch

haiku_bot = dspy.ReAct(
    "location -> haiku",
    tools=[web_search, bash_fetch],
)

result = haiku_bot(location="Camp Meeker")
print(result.haiku)
```

### Basic Usage with dspy.RLM

The same functions go into `dspy.RLM`'s `tools` list, where they are injected
into the REPL namespace and callable from the model's generated code:

```python
import dspy
from dspy_agent_tools import bash_exec, read_file, edit_file

rlm = dspy.RLM(
    "task -> report",
    tools=[bash_exec, read_file, edit_file],
)

result = rlm(task="Add a docstring to every public function in src/utils.py")
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE -->

## Usage

### With dspy.ReAct

`dspy.ReAct` runs an agentic reasoning loop over your tools. Pick the tools
the agent should see — a coding agent might get the file and shell tools, a
research agent the search and fetch tools:

```python
import dspy
from dspy_agent_tools import (
    web_search,
    bash_fetch,
    bash_curl,
    web_news,
)

researcher = dspy.ReAct(
    "question -> answer, sources",
    tools=[web_search, web_news, bash_fetch, bash_curl],
    max_iters=8,
)

result = researcher(question="What changed in DSPy 3.1?")
print(result.answer)
```

Each tool is presented to the LM by name, docstring, and parameters. The
docstrings shipped in this package are already tuned for that presentation —
they steer the model toward the right tool (e.g. `bash_fetch` for readable
pages, `bash_curl` for raw API responses).

### With dspy.RLM

`dspy.RLM` executes model-written Python in a sandboxed REPL and hands the
model the same functions to call from inside its code:

```python
import dspy
from dspy_agent_tools import (
    bash_exec,
    bash_ripgrep,
    bash_find,
    read_file,
    write_file,
    edit_file,
)

coder = dspy.RLM(
    "task -> summary",
    tools=[bash_exec, bash_ripgrep, bash_find, read_file, write_file, edit_file],
    max_iters=20,
)

result = coder(task="Rename the class SessionManager to Session across the repo")
```

A full coding-agent toolbox, all in one import:

```python
from dspy_agent_tools import (
    bash_exec,
    bash_curl,
    bash_fetch,
    bash_find,
    bash_find_batch,
    bash_glob,
    bash_glob_batch,
    bash_ls,
    bash_ripgrep,
    bash_ripgrep_batch,
    edit_file,
    read_file,
    write_file,
    web_news,
    web_search,
)
```

### With MCP servers

`MCP` is one wrapper for every transport. It starts the server, converts every
tool it advertises into a `dspy.Tool`, and returns them as an `MCPTools` list.
The server keeps running while the tools are referenced, so repeated calls
reuse one session:

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

Remote servers use the same wrapper. `url=` selects Streamable HTTP, and
`transport="sse"` selects the legacy HTTP+SSE transport:

```python
tools = MCP(
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer <token>"},
)

legacy = MCP(url="https://example.com/sse", transport="sse")
```

- `MCP(command=..., args=[...])` runs a local **stdio** server; `env=` and
  `cwd=` configure its environment and working directory.
- `MCP(url=...)` connects over **Streamable HTTP**, the transport used by most
  hosted MCP servers.
- `MCP(url=..., transport="sse")` connects over legacy **HTTP+SSE**.
- `MCP(transport=factory)` uses a custom async transport: `factory` is a
  zero-argument callable returning an async context manager that yields a
  read/write pair.
- `transport="auto"` (the default) infers the transport from `command=` vs
  `url=`; pass `"stdio"`, `"http"`, or `"sse"` to be explicit.

Every transport accepts `startup_timeout` (seconds to wait for initialization
and tool listing, default 60), `name_prefix` (prepended to every tool name),
and `result_mode`: `"text"` (default) flattens MCP text content to strings,
`"structured"` returns the server's `structuredContent` when it provides it.
The stdio transport ignores the HTTP-only options.

Remote transports (`url=`, `transport="http"` or `"sse"`) additionally accept:

- `headers` — extra HTTP headers such as an `Authorization` bearer token.
- `timeout` — HTTP timeout; defaults to 30 s for Streamable HTTP and 5 s for
  legacy SSE.
- `sse_read_timeout` — how long to wait on the server stream (default 300 s).
- `auth` — an `httpx`/`httpx2` auth handler, e.g. an MCP OAuth client
  provider.
- `http_client_factory` — zero-argument callable returning an async HTTP
  client, created and closed on the connection's event loop. For Streamable
  HTTP it replaces the default client, so `headers`, `timeout`, and `auth`
  are then ignored; for legacy SSE it is passed to the SDK as
  `httpx_client_factory` alongside the other options.
- `terminate_on_close` — ask the server to terminate the HTTP session on close
  (Streamable HTTP only, default `True`).

For legacy SSE, `headers`, `timeout`, `sse_read_timeout`, and `auth` are only
forwarded when the installed MCP SDK's `sse_client` accepts them.

Both paths work out of the box: `agent(...)` synchronously converts the
async MCP calls (scoped to those tools, no global DSPy setting), and
`await agent.acall(...)` runs fully asynchronously in async applications.
No cleanup is required: the session keeps running while the tools or the
`MCPTools` list are referenced, then closes when they are garbage-collected,
with an interpreter-exit fallback. Call `tools.close()` only to stop it
earlier.

- Call `MCP` once per server and concatenate the results for several servers;
  use `name_prefix="fs_"` when servers expose identically named tools.
- The return value, `MCPTools`, is a `list` subclass: passing it as `tools=`,
  concatenating with `+` on either side, and unpacking all work. It is also a
  context manager (`with MCP(...) as tools:`) that closes the server on exit.

### Tool discovery with Toolbox

A 46-tool server such as GitHub's MCP endpoint costs roughly 14k tokens of
function definitions on every turn when every tool is passed as its own
`dspy.Tool`. `Toolbox` keeps the catalog in the Python process and exposes
three discovery tools instead. It accepts any mix of MCP servers, regular
`dspy.Tool` objects, and plain sync or async Python functions:

```python
from dspy_agent_tools import MCP, Toolbox, bash_exec, read_file


def lookup_ticket(ticket_id: str) -> dict:
    """Look up one support ticket by its id."""
    ...


catalog = Toolbox(
    [
        MCP(
            url="https://api.githubcopilot.com/mcp/",
            headers={"Authorization": f"Bearer {token}"},
        ),
        bash_exec,
        read_file,
        lookup_ticket,
    ],
    mode="search",
)

agent = dspy.ReActV2("question -> answer", tools=catalog)
```

`mode="search"` returns three tools instead of the whole catalog:

| Tool | Purpose |
| --- | --- |
| `search_tools(query, limit=5)` | BM25 ranking over tool names, descriptions, and parameter names; returns names with their required arguments. |
| `get_tool_details(name)` | Full description and JSON input schema for one tool. |
| `call_tool(name, arguments)` | Runs that tool; MCP calls stay on the live session. |

The catalog stays in the Python process, so only the tools the agent searches,
inspects, or calls are sent to the model. `mode="code"` returns the same three
tools with structured results — `list[dict]` from `search_tools`, `dict` from
`get_tool_details` — so `dspy.RLM` code can compose calls without parsing
text:

```python
worker = dspy.RLM(
    "task -> report",
    tools=Toolbox(
        [MCP(url="https://api.githubcopilot.com/mcp/", headers={...}), bash_exec],
        mode="code",
    ),
)
```

#### Search mode vs code mode

Both modes expose the same three discovery tools and the same local BM25
catalog; the difference is who orchestrates the calls and how results move
through the context.

| | `mode="search"` | `mode="code"` |
| --- | --- | --- |
| Agent | `dspy.ReAct` / `dspy.ReActV2` and other tool-calling modules | `dspy.RLM` or another agent that executes Python |
| Return shape | text (`str`) | structured (`list[dict]`, `dict`) |
| Orchestration | The model picks one tool per turn | The model writes code that can call many tools |
| Intermediate results | Returned to the model as observations | Stay in the interpreter unless printed |
| Best for | A few lookups with small results | Multi-step workflows, loops, filtering, large payloads |
| Cost profile | Saves schema tokens; observations still enter context | Saves schema tokens and can keep large results out of context |
| Requirement | None beyond DSPy | A working code interpreter |

Use `mode="search"` when:

- You are building a ReAct-style agent.
- The task usually needs one to three tool calls with small outputs.
- You want the safest option: the model never writes or executes code.
- You are unsure which mode to pick — start here.

Use `mode="code"` when:

- You are building an RLM that can write Python.
- The task needs several calls combined with Python logic (filter, rank,
  join, aggregate).
- Tool results are large and only a summary needs to reach the model. In RLM,
  `search_tools`, `get_tool_details`, and `call_tool` are Python callables;
  intermediate values stay in the REPL unless you `print()` them or assign
  them to the final output.

Skip both modes when the catalog is small (roughly fewer than ten tools) and
the task is straightforward. The discovery round trip — search, inspect, then
call — can cost more than just passing the function definitions directly as
`tools=`.

- Plain functions and `dspy.Tool` instances are catalogued from their DSPy
  metadata: function name, docstring, type hints, and defaults.
- Names must be unique (case-insensitively); pass `name_prefix="..."` or rename
  the function. `MCP(..., name_prefix=...)` still works for MCP-side names.
- `Toolbox` owns its sources, so `catalog.close()` (or a `with` block) stops
  every MCP server it was given. Addition (`catalog + [other_tool]`) keeps
  that ownership.
- `MCP` always returns the eager tool list; pass it to `Toolbox` to get
  discovery mode.

### Batching

Search tools come in single and `_batch` variants. A batch takes
`list[dict]` — one spec per search, each with `pattern`, `path`, and
`options` — and returns labeled sections. Use it when several independent
lookups are needed; it saves round trips and shares one timeout across the
batch:

```python
bash_ripgrep_batch(
    [
        {"pattern": r"def main", "path": "src", "options": "-n"},
        {"pattern": "TODO|FIXME", "options": "-n -i"},
    ]
)
# == def main in src ==
# src/cli.py:12:def main(args):
# ...
# == TODO|FIXME in . ==
# ...
```

`bash_find_batch` and `bash_glob_batch` work the same way for path lookups.

### Safety Notes

- `bash_exec` runs arbitrary shell commands with the caller's permissions.
  Sandbox or restrict the agent before giving it that power.
- File tools are guarded by **snapshots**: `read_file` returns a short hash of
  the file's content, and `edit_file`/`write_file` reject the change if the
  file has changed since it was read, so concurrent edits can't be silently
  overwritten.
- Non-zero exits, timeouts, and failed transfers are reported inline as
  `"[exit code N]"` / `"[command timed out ...]"` notices rather than raised,
  so an agent can read and recover from failures.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- TOOL REFERENCE -->

## Tool Reference

### Bash (`dspy_agent_tools.bash`)

| Tool | Use it for |
| --- | --- |
| **`bash_exec`** | Run an arbitrary shell command (`command`, `cwd`, `timeout`). The fallback tool for anything without a dedicated tool: builds, tests, git, python snippets. |
| **`bash_ripgrep`** | Search file **contents** with a regex (`pattern`, `path`, `options`). rg-style flags, ripgrep semantics — hidden/gitignored paths skipped by default. |
| **`bash_ripgrep_batch`** | Many content searches in one call. `list[dict]` specs; labeled sections; one shared timeout. |
| **`bash_find`** | Find file/directory **names** with a regex (fd engine). `-e py`, `-t f`, `-d 2`, `-E <glob>`. |
| **`bash_find_batch`** | Many path lookups in one call. |
| **`bash_glob`** | Match paths against a glob — `"*.py"` at any depth, `"src/*.py"` anchored. |
| **`bash_glob_batch`** | Many glob lookups in one call. |
| **`bash_ls`** | List one directory's entries in long format. |
| **`bash_curl`** | Fetch a URL and return the **raw** response body — API JSON, headers, binaries. |
| **`bash_fetch`** | Fetch a web page and return **readable markdown** — nav, ads, cookie banners stripped, title kept. Prefer over `bash_curl` when the goal is reading. |

### Files (`dspy_agent_tools.files`)

| Tool | Use it for |
| --- | --- |
| **`read_file`** | Read a file; returns `(numbered_content, snapshot)`. Quote text WITHOUT the `" N | "` prefixes in later calls. |
| **`write_file`** | Write an entire file; optionally snapshot-guarded. Returns the new snapshot. |
| **`edit_file`** | Edit under snapshot control. Exactly one addressing mode per call: `block="MyClass.method"` (tree-sitter, any language in tree-sitter-language-pack), `start_line`/`end_line` (inclusive 1-based), or `old_text` (exact, must match once; auto-repairs near-misses down to `min_similarity=0.8` and strips pasted line-number prefixes). |

### Search (`dspy_agent_tools.search`)

| Tool | Use it for |
| --- | --- |
| **`web_search`** | Web search across ddgs engines in failover order. Returns `{"query", "results": [{"title", "href", "body"}], "count", "status"}`. `timelimit`: `"d"`, `"w"`, `"m"`, `"y"`, or `None`. |
| **`web_news`** | News search; results add `source` and `date`. Same `timelimit` values and `status` contract. |

`status` is `ok`, `no_results` (engines answered but found nothing), or
`error` (no engine answered; `error` then carries the attempt count). An
engine that returns no results is retried with a fresh client, because ddgs
randomizes its browser profile and a mobile profile can hide results the
parser cannot read. An engine with a transport failure is skipped for the
rest of the search and the next engine is tried. The whole search shares a
45 s budget.

Shared behaviour across the bash tools:

- Output is returned in full (no inline truncation). Redirect to a file, pipe through `head`/`grep`, or slice the returned value when output may be huge.
- Wall time is appended as `(Ns)`.
- Timeouts kill the whole process group and return partial output.
- Bad regexes and unknown flags come back as `[Error]` text instead of raising.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- DEVELOPMENT -->

## Development

### Code Quality

This project uses several tools to maintain code quality:

- **Ruff:** Linting and formatting
- **isort:** Import sorting
- **pytest:** Testing framework
- **ty:** Type checking
- **deptry:** Dependency analysis

**Available commands:**

```sh
# Run all quality checks
uv run poe clean-full

# Individual checks
uv run poe lint          # Ruff linting
uv run poe format        # Ruff formatting
uv run poe sort          # Import sorting
uv run poe typecheck     # Type checking
uv run poe deptry        # Dependency analysis
```

### Testing

Run tests using pytest:

```sh
# Run all tests
uv run pytest

# Run with coverage
uv run poe test-cov

# Run specific test
uv run pytest path/to/test.py::test_name
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- CONTRIBUTING -->

## Contributing

Quick workflow:

1. Fork and branch: `git checkout -b feature/name`
2. Make changes
3. Run checks: `uv run poe clean-full`
4. Commit and push
5. Open a Pull Request

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- LICENSE -->

## License

MIT (as declared in `pyproject.toml`).

---

<div align="center">
  <p>
    <sub>Built by <a href="https://github.com/thememium">thememium</a></sub>
  </p>
</div>
