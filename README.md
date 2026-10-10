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

Three families of tools:

- **Bash** — shell execution, file search (ripgrep/fd-style, no binaries needed), directory listing, and URL fetching
- **Files** — read, write, and edit files with snapshot-based optimistic concurrency and tree-sitter block addressing
- **Search** — DuckDuckGo web and news search

Tools are just Python functions, exactly as
[DSPy's docs describe](https://dspy.ai/current/getting-started/react-and-tools/#tools-are-just-python-functions):
DSPy reads the name, type-hinted parameters, and docstring of each function to
assemble the instructions it sends to the LM. These docstrings are written for
that purpose — each one tells the model WHAT the tool does, WHEN to use it
instead of a sibling tool, and HOW to pass arguments.

Requires **Python 3.13+** and **DSPy 3.1+**.

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
| **`web_search`** | DuckDuckGo web search. Returns `{"query", "results": [{"title", "href", "body"}], "count"}`. `timelimit`: `"d"`, `"w"`, `"m"`, `"y"`, or `None`. |
| **`web_news`** | DuckDuckGo News search; results add `source` and `date`. Same `timelimit` values. |

Shared behaviour across the bash tools:

- Output is capped at ~30KB (first 20KB + last 10KB kept) with a truncation notice.
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
