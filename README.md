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

Core tools, optional bridges, and progressive discovery:

- **Bash** — shell execution, file search, directory listing, and URL fetching
- **Files** — read, write, and edit with snapshot guards and tree-sitter addressing
- **Search** — web and news search with engine failover and explicit status
- **CLI** — shell-free typed facade over any executable; discovery or explicit `Command` tools. See [docs/cli.md](docs/cli.md).
- **MCP** (optional) — local stdio or remote HTTP/SSE servers as `dspy.Tool`s. See [docs/mcp.md](docs/mcp.md).
- **Skills** — Agent Skills folders as progressive-disclosure tools. See [docs/skills.md](docs/skills.md).
- **Patch** — add tools to a built agent and inject one guidance section per tool family. See [docs/patch.md](docs/patch.md).
- **Browser** (optional) — headless Chrome as one `dspy.Tool` list, with optional recording and search. See [docs/browser.md](docs/browser.md).
- **Toolbox** — mix MCP servers, tools, and functions; optional progressive discovery.

Tools are just Python functions, exactly as
[DSPy's docs describe](https://dspy.ai/current/getting-started/react-and-tools/#tools-are-just-python-functions):
DSPy reads the name, type-hinted parameters, and docstring of each function to
assemble the instructions it sends to the LM. These docstrings are written for
that purpose — each one tells the model WHAT the tool does, WHEN to use it
instead of a sibling tool, and HOW to pass arguments.

Requires **Python 3.13+**; run agents with **DSPy 3.1+** (the `[browser]` and
`[mcp]` extras install it).

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

### Optional Installs

The browser and MCP bridges are optional extras. Importing `Browser` or `MCP`
works without them; calling either raises `ImportError` with the install hint.

```bash
uv add 'dspy-agent-tools[browser]'
# or: pip install 'dspy-agent-tools[browser]'
```

`Browser()` drives an installed Chrome headless through pydoll; no separate
browser download is needed. Recording uses `ffmpeg` from `PATH`; install the
extra to bundle one, otherwise `browser_record_start` returns an error:

```bash
uv add 'dspy-agent-tools[ffmpeg]'
# or: pip install 'dspy-agent-tools[ffmpeg]'
```

The MCP bridge pulls in DSPy, `httpx2`, and MCP SDK 2.x:

```bash
uv add "dspy-agent-tools[mcp]"
```

Ready to build an agent? [Usage](#usage) shows complete `dspy.ReAct` and
`dspy.RLM` examples.

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
    delete_file,
    edit_file,
)

coder = dspy.RLM(
    "task -> summary",
    tools=[
        bash_exec,
        bash_ripgrep,
        bash_find,
        read_file,
        write_file,
        delete_file,
        edit_file,
    ],
    max_iters=20,
)

result = coder(task="Rename the class SessionManager to Session across the repo")
```

### With the headless browser

`Browser()` returns the core browser toolset as one list of `dspy.Tool`
objects. Nothing is created or cleaned up by hand:

```python
import dspy
from dspy_agent_tools import Browser

agent = dspy.ReAct(
    "task -> result",
    tools=Browser(),
    max_iters=12,
)

result = agent(task="Open https://example.com and summarize the page")
```

`Browser(include_search=True)` appends `web_search` for research agents, and
`Browser(include_recording=True)` adds video recording. The individual
functions remain available when you want a hand-picked list.

For a server that needs one browser per user or session, bind the tool list to
a session:

```python
from dspy_agent_tools import Browser, BrowserSession

session = BrowserSession()
tools = Browser(session=session)  # or session.tools()
tools.close()  # stops this session only
```

See [docs/browser.md](docs/browser.md) for actions, snapshots and refs, tabs,
network capture, cookies and profiles, video recording, and URL policy.

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
    delete_file,
    edit_file,
    read_file,
    write_file,
    web_news,
    web_search,
)
```

### With MCP servers

`MCP` starts a server, converts every tool it advertises into a `dspy.Tool`,
and returns them as an `MCPTools` list. Use `command=` for a local stdio server
or `url=` for Streamable HTTP; `transport="sse"` selects legacy HTTP+SSE, and
`transport=<factory>` takes any custom async transport.

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

Remote servers use the same wrapper:

```python
hosted = MCP(
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer <token>"},
)
```

`MCPTools` is a `list` subclass, so it composes with other tools; it is also a
context manager with `.close()`. `agent(...)` works synchronously and
`await agent.acall(...)` runs fully asynchronously. See
[docs/mcp.md](docs/mcp.md) for transport selection, result modes, startup
retries, HTTP options, and lifecycle.

### With any CLI

`CLI` wraps an executable that has no MCP server. By default it exposes a
two-tool discovery facade (`get_command_help`, `run_command`) and reads the
program's own help on demand; `include_search=True` adds `search_commands`,
and `commands=[Command(...)]` defines typed tools explicitly instead.

```python
import dspy
from dspy_agent_tools import CLI, bash_exec

gh = CLI("gh")
agent = dspy.ReAct("question -> answer", tools=gh + [bash_exec])

worker = dspy.RLM("task -> report", tools=CLI("docker", mode="code"))
```

Discovery starts on the first tool call; help pages are cached, and
`tools.refresh()` drops them. Only the configured executable is reachable and
no shell is involved. See [docs/cli.md](docs/cli.md) for the tool set,
discovery budget, help and JSON probing, execution options, and explicit
`Command`/`Argument`/`Option` definitions.

### With Agent Skills

`Skills` loads folders that follow the [Agent Skills
specification](https://agentskills.io/specification) — the same `SKILL.md`
folders Claude Code, Codex, and other clients use. Each immediate subdirectory
with a `SKILL.md` is discovered at startup, so only its name, description, and
declared `allowed-tools` enter the model context; the body loads when the agent
activates the skill.

```python
import dspy
from dspy_agent_tools import Skills, bash_exec

skills = Skills("~/.agents/skills", ".agents/skills")

agent = dspy.ReAct("question -> answer", tools=skills + [bash_exec])
worker = dspy.RLM("task -> report", tools=Skills("./skills", mode="code"))
```

`Patch(agent, tools=Skills("./skills"))` appends the `<available_skills>` catalog
and the skill tools to an agent that was already built. It rebuilds the inner
predictor, so it works for `dspy.ReAct`, `dspy.ReActV2`, and `dspy.RLM`, is
idempotent, and returns the agent:

```python
from dspy_agent_tools import Patch, Skills, bash_exec, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec, read_file])
Patch(agent, tools=Skills("./skills"))
```

The injected block names the host-command and URL-reading tools the agent
actually has. When it has neither, the model reports the exact command for you
to run instead of inventing output. See [docs/skills.md](docs/skills.md) for
the skill layout, validation rules, sandbox for bundled scripts, and every
constructor option.

### Adding tools to an existing agent

`Patch(agent, tools=..., guide=None, drop=...)` extends an agent that was
already built: it appends the tools and one short policy section per tool
family present to its instructions. Any source works — `Skills(...)`,
`MCP(...)`, `CLI(...)`, `Browser()`, `Toolbox(...)`, `dspy.Tool` objects, or
plain functions:

```python
from dspy_agent_tools import CLI, Patch, Skills, bash_exec, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec])
Patch(agent, tools=[read_file, Skills("./skills"), CLI("gh")], guide="Never commit.")
```

The agent is rebuilt in place and returned, so it keeps working and chains.
Each section names the tools the agent actually has and carries what a tool
cannot say about itself: which family to reach for (search, fetch, browser),
how it behaves (no shell in a command tool, no process spawning from the RLM
REPL), and how to load tools on demand (`search_tools` → `call_tool`).
`describe=False` appends tools without sections.

Patch keeps its guidance in one delimited region of the instructions and
recomputes it on every call: edited `guide` text replaces the old copy, the
last guide carries over to later calls (`guide=""` clears it), and a family
whose heading you already wrote yourself (`## Shell`) is left to you. Patching
twice with the same arguments changes nothing. Rebuilding replaces the inner
predictor, so few-shot demos from an optimizer are dropped with a warning —
patch before compiling, or recompile afterwards. See [docs/patch.md](docs/patch.md).

### Tool discovery with Toolbox

`Toolbox` accepts any mix of MCP servers, `dspy.Tool` objects, and sync or
async Python functions, and flattens nested lists. With no `mode`, it returns
every tool eagerly. With `mode="search"` or `mode="code"`, it keeps the
catalog in-process and exposes three discovery tools instead — useful when
every schema would cost tokens on every turn.

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

The catalog stays in the Python process, so only tools the agent searches,
inspects, or calls reach the model. `mode="code"` returns structured results
(`list[dict]` from `search_tools`, `dict` from `get_tool_details`) so
`dspy.RLM` code can compose calls without parsing text:

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
| Agent | `dspy.ReAct` / `dspy.ReActV2` | `dspy.RLM` or another Python-executing agent |
| Return shape | text (`str`) | structured (`list[dict]`, `dict`) |
| Orchestration | One tool per turn | Code can call many tools |
| Intermediate results | Returned as observations | Stay in the REPL unless printed |
| Best for | A few small lookups | Multi-step workflows, loops, large payloads |
| Cost profile | Saves schema tokens; observations still enter context | Saves schema tokens; large results can stay out of context |
| Requirement | None beyond DSPy | A working code interpreter |

Use `mode="search"` when:

- You are building a ReAct-style agent.
- The task needs a few calls with small outputs.
- You want the model to never write or execute code.
- You are unsure which mode to pick — start here.

Use `mode="code"` when:

- You are building an RLM that can write Python.
- The task needs several calls combined with Python logic.
- Results are large and only a summary needs to reach the model.

Skip discovery for small catalogs (roughly fewer than ten tools); the
search/inspect/call round trip can cost more than passing definitions
directly. Omit `mode` for the full load while keeping `Toolbox`'s source
ownership, duplicate-name checks, and `name_prefix` handling.

- Plain functions and `dspy.Tool` instances use DSPy metadata: name,
  docstring, type hints, and defaults.
- Names must be unique case-insensitively; pass `name_prefix="..."` or rename.
  `MCP(..., name_prefix=...)` still works for MCP-side names.
- `Toolbox` owns its sources: `catalog.close()` or `with` stops every MCP
  server; `catalog + [other_tool]` keeps that ownership.
- `MCP` always returns the eager list; pass it to `Toolbox` for discovery.

### Batching

`_batch` variants accept `list[dict]` specs with `pattern`, `path`, and
`options`, returning labeled sections with one shared timeout. Use them for
independent lookups to save round trips:

```python
bash_ripgrep_batch(
    [
        {"pattern": r"def main", "path": "src", "options": "-n"},
        {"pattern": "TODO|FIXME", "options": "-n -i"},
    ]
)
```

`bash_find_batch` and `bash_glob_batch` work the same way for path lookups.

### Safety Notes

- `bash_exec` runs arbitrary shell commands with the caller's permissions.
  Sandbox or restrict the agent before giving it that power.
- File tools are guarded by **snapshots**: `read_file` returns a short hash of
  the file's content, and `edit_file`/`write_file`/`delete_file` reject the
  change if the file has changed since it was read, so concurrent edits can't
  be silently overwritten.
- Browser tools drive a real headless Chrome with the caller's network reach,
  and `browser_evaluate` runs arbitrary JavaScript in the page. Treat page
  content as untrusted input, and use one `BrowserSession` per user or session
  on a shared server so cookies and tabs do not leak between callers.
- Non-zero exits, timeouts, and failed transfers are reported inline as
  `"[exit code N]"` / `"[command timed out ...]"` notices rather than raised,
  so an agent can read and recover from failures.
- Skill scripts are sandboxed at the OS level when `bwrap`, `firejail`, or
  `sandbox-exec` is available. Without one they fall back to process
  confinement (allowlisted interpreter, path guard, scrubbed environment,
  timeout), which is not a security boundary — pass `sandbox="required"` when
  skill scripts are untrusted.

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
| **`delete_file`** | Delete a file; optionally snapshot-guarded. Refuses directories; a missing path raises `FileNotFoundError`. |
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

### CLI (`dspy_agent_tools.cli`)

| Tool | Use it for |
| --- | --- |
| **`CLI`** | Return a `CLITools` list for one executable. By default it contains `get_command_help` and `run_command`; `include_search=True` adds `search_commands`; `commands=[...]` returns one typed tool per explicit `Command` and skips discovery. Names are prefixed by `name_prefix` (default: the executable name). |
| **`get_command_help`** | Read one path's usage, subcommands, parsed arguments and flags, JSON help, and fallback raw help. `command=""` returns the root command list. |
| **`search_commands`** | Optional (`include_search=True`): rank command paths by name and description. |
| **`run_command`** | Run one resolved path with raw argv strings. No shell; only the configured executable is reachable. |
| **`Command`** | Define one explicit command: path, tool name, description, `Argument`/`Option` declarations, `fixed` argv, and `ok_codes`. |
| **`Argument`** | Declare a required or optional positional for a `Command`. |
| **`Option`** | Declare a flag or value option for a `Command`, including repeatable flags and choices. |
| **`CLITools`** | `list` subclass with `.refresh()`, which drops cached help pages in discovery mode. |

`mode="search"` returns text for `dspy.ReAct`; `mode="code"` returns
`list[dict]` / `dict` for `dspy.RLM`. The parsed flag table is advisory; the
raw help is authoritative. See [docs/cli.md](docs/cli.md).

### MCP (`dspy_agent_tools.mcp`)

| Tool | Use it for |
| --- | --- |
| **`MCP`** | Connect to an MCP server and return its tools. `command=` runs a local stdio server; `url=` connects over Streamable HTTP; `transport="sse"` selects legacy HTTP+SSE; `transport=<factory>` takes a custom async transport. |
| **`MCPTools`** | `list` subclass of `dspy.Tool` objects backed by one live session; supports `+`, unpacking, `with`, `async with`, and `.close()`. |

`name_prefix`, `result_mode`, `startup_timeout`, and `retry_timeout` tune the
conversion. The server stops when the tools or list are no longer referenced.
See [docs/mcp.md](docs/mcp.md).

### Skills (`dspy_agent_tools.skills`)

| Tool | Use it for |
| --- | --- |
| **`Skills`** | Scan one or more folders for `SKILL.md` skills and return a `SkillTools` list. `mode="search"` returns text; `mode="code"` returns dictionaries for `dspy.RLM`. |
| **`list_skills`** | Names, descriptions, declared `allowed-tools`, and bundled files for every skill. |
| **`activate_skill`** | The full `SKILL.md` body, bundled file list, and declared `allowed-tools` (code mode also returns `license`, `compatibility`, and `metadata`). |
| **`read_skill_resource`** | One file from `references/` or `assets/`, relative to the skill folder; escapes are rejected. |
| **`run_skill_script`** | One file from `scripts/` with an argv list, run in the sandbox; omitted when `scripts=False`. |
| **`SkillTools`** | `list` subclass with `.skills`, `.errors`, `.catalog()`, and `.section(tools=())`. |

`Patch(agent, tools=skills)` appends the catalog and tools to an existing
`dspy.ReAct`, `dspy.ReActV2`, or `dspy.RLM`. See [docs/skills.md](docs/skills.md).

### Patch (`dspy_agent_tools.patch`)

| Tool | Use it for |
| --- | --- |
| **`Patch`** | Add `tools=` to an existing `dspy.ReAct`, `dspy.ReActV2`, or `dspy.RLM`, remove `drop=`, and inject one guidance section per tool family the agent holds. `guide=` adds your own policy before those sections; `describe=False` appends tools without them. |

Tools are accepted as `dspy.Tool` objects, plain functions, or lists of them.
The sections live in `dspy_agent_tools.guide`, one per family. See
[docs/patch.md](docs/patch.md).

### Browser (`dspy_agent_tools.browser`)

| Tool | Use it for |
| --- | --- |
| **`Browser`** | Return the core browser tools as one `BrowserTools` list of `dspy.Tool` objects. `include_recording=True` adds video recording, `include_search=True` adds `web_search`, and `.close()` stops that list's browser session. Pass `session=BrowserSession(...)` for per-user isolation; `session.tools()` is equivalent. |
| **`browser_open`** | Navigate to a URL and return its snapshot in one call. `format="nodes"` is the default; `format="text"` returns the compact tree, and `format="both"` returns both. |
| **`browser`** | Run one action: `navigate`, `click`, `type`, `press`, `hover`, `select`, `scroll`, `back`, `forward`, `reload`, or `wait`. `target` accepts an `@ref`, CSS, XPath, `text=Exact`, or `text*=Partial`; `value` carries the URL, text, key, option value, scroll direction, or seconds. |
| **`browser_snapshot`** | Return `{url, title, node_count, truncated}` plus `nodes`, `text`, or both selected by `format` (default `nodes`). Nodes carry `ref` (`@e1`), `role`, `name`, `tag`, `depth`, and relevant `value`/`href`/`checked`/`disabled`. Every snapshot clears and reassigns refs. |
| **`browser_tabs`** | List, open, switch, and close tabs. `switch` takes an index or a URL/title substring; closing the last tab stops the browser. |
| **`browser_network`** | Inspect recorded requests, return a response body by request id, or clear the record. |
| **`browser_cookies`** | Read, write, clear, save, and load cookies for the current browser context. |
| **`browser_screenshot`** | Save a PNG/JPEG/WebP (temp file by default) and return `path`, `format`, `bytes`; `full_page` captures beyond the viewport, `as_base64` returns the image inline. |
| **`browser_evaluate`** | Run JavaScript with a `return` statement and get the value by value (`return document.title`). `await_promise` waits for promises. |
| **`browser_record_start`** / **`browser_record_stop`** | Optional (`include_recording=True`): start and stop a CDP screencast recording. MP4 (H.264) by default, WebM/VP9 on `.webm`; uses `ffmpeg` from PATH or the optional `[ffmpeg]` extra. |
| **`browser_close`** | Stop this tool list's browser session early; the next call starts a fresh one. An active recording is encoded and returned under `recording`. |
| **`BrowserSession`** | Advanced: one lazily started headless browser per user/session with the same methods, plus `.tools()` for a session-bound `dspy.Tool` list and `allowed_schemes`/`url_policy` for navigation restrictions. |

Chrome runs headless by default. Actions return `{"status": "ok" | "error", ...}`;
on failure `error` carries the reason, and a stale `@ref` asks for a fresh
snapshot. See [docs/browser.md](docs/browser.md).

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
