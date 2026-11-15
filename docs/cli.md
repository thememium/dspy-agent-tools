# CLI tools

`dspy_agent_tools.cli` turns an executable that has no MCP server into ordinary
`dspy.Tool` functions. A plain CLI exposes no machine-readable schema, so its
own help output is the schema. `CLI` returns a discovery facade by default, or
one typed tool per command when you define the commands explicitly.

Requires DSPy 3.1+ (the `[browser]` or `[mcp]` extra installs it, or install
`dspy` directly).

## Quick start

```python
import dspy
from dspy_agent_tools import CLI, bash_exec

gh = CLI("gh")
agent = dspy.ReAct("question -> answer", tools=gh + [bash_exec])
result = agent(question="List the open pull requests in this repository")

worker = dspy.RLM("task -> report", tools=CLI("docker", mode="code"))
```

`CLI` returns a `CLITools` list, so it can be passed as `tools=`,
concatenated with other tools, or unpacked.

## Default facade

Without `commands=`, `CLI` exposes the program's help as a small discovery
surface:

| Tool | Purpose |
| --- | --- |
| `get_command_help(command="")` | Usage, subcommands, parsed arguments and flags, JSON help, and raw help for one path. `command=""` returns the root command list. |
| `run_command(command="", args=None)` | Execute `[executable, *base_args, *command_path, *args]`; no shell. |
| `search_commands(query, limit=8)` | Optional (`include_search=True`): rank command paths by name and description. |

Names are prefixed with `name_prefix`; the default is a slug of the
executable's base name plus an underscore, so `CLI("gh")` produces
`gh_get_command_help`, `gh_run_command`, and (when enabled)
`gh_search_commands`.

Discovery starts on the first tool call, not when `CLI()` is called. Every
help page is cached, and `tools.refresh()` drops the cache so the next call
starts from the root again.

### How discovery expands

`get_command_help(command="")` fetches the root help. Resolving `pr list`
fetches `pr` and then `pr list` only when needed; `run_command` resolves its
path lazily, so fetching help first is optional.

With `include_search=True`, one search may fetch at most `max_expands` help
pages (default 8) to at most `max_depth` tree levels (default 3). Searching
for `pr list` typically costs two help calls (root, then `pr`); asking for its
details then fetches `pr list`. Nothing walks the whole tree.

```python
git = CLI("git")
tools = {tool.name: tool for tool in git}

tools["git_get_command_help"](command="")       # root command list
tools["git_get_command_help"](command="status") # usage + flags
tools["git_run_command"](command="status", args=["--short"])

# Name lookup over a large command tree:
git = CLI("git", include_search=True)
```

`search_commands` ranks known commands by exact/prefix name matches and
description matches, then expands the best candidates within the budget.
`limit` is clamped to 1–50. An empty query lists root commands.

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `executable` | required | Program resolved through `PATH` (or a path). Only this program is reachable. |
| `commands` | `None` | Explicit `Command` definitions; skips discovery. Must contain at least one command. |
| `base_args` | `()` | Fixed arguments inserted before the command path, e.g. `["-m", "mytool"]`. |
| `mode` | `"search"` | Discovery result shape: text for ReAct, dictionaries for RLM. Ignored with `commands=`. |
| `name_prefix` | executable name + `_` | Prepended to every tool name. |
| `cwd` | `None` | Working directory for help calls and execution. |
| `env` | `None` | Extra environment variables for help and execution. Help also forces non-interactive pager settings. |
| `timeout` | `300` | Deadline in seconds for `run_command`; `0` disables it. |
| `help_timeout` | `10` | Deadline for each help call. |
| `max_expands` | `8` | Maximum help pages one search may fetch. |
| `max_depth` | `3` | Maximum command-tree levels one search may walk. |
| `max_help_chars` | `20_000` | Cap for JSON/raw help in `get_command_help`; `0` omits it. Parsed flags are unaffected. |
| `help_flag` | `"--help"` | Help invocation tokens; `None` skips help and runs the command. |
| `json_flag` | `None` | Optional machine-readable flag to probe, e.g. `"--json"`. |
| `max_output_chars` | `0` | Cap one command result; `0` keeps everything. |
| `include_search` | `False` | Add `search_commands` to the discovery tools. |

`CLI()` raises `FileNotFoundError` when the executable is not found. Invalid
values (`mode`, out-of-range caps, `help_timeout <= 0`, empty `help_flag`
tokens, a `json_flag` that does not start with `-`) raise `ValueError`.

## Help behavior

### `help_flag`

`help_flag` controls how discovery asks for help. Use `"-h"`, `"help"`, or a
token sequence such as `("help", "commands")`; `None` skips the help call
entirely and runs the command itself.

If the help invocation is rejected as an unknown flag or returns nothing, the
command itself runs so the agent can inspect whatever it prints. Discovery
also forces non-interactive settings (`PAGER=cat`, `MANPAGER=cat`,
`GIT_PAGER=cat`, `GH_PAGER=cat`, `NO_COLOR=1`, `TERM=dumb`, `COLUMNS=100`) so
a pager cannot block the call.

Some programs (git is the common one) render a man page for `--help`. That
rendering is returned as raw help rather than a reliable flag table; pass
`help_flag="-h"` for those programs to get their short help instead.

### `json_flag`

`json_flag` is disabled by default. Pass `json_flag="--json"` to probe a
machine-readable output:

- On a command run, `--json` is inserted after the command path:
  `[executable, *base_args, *path, json_flag, *args]`.
- On a help call, it is inserted after the help tokens:
  `[executable, *base_args, *path, *help_tokens, json_flag]`.

If the CLI rejects the flag as unknown, the attempt is retried without it.
Real failures are not retried. JSON help is kept alongside the text help used
for discovery, so `search_commands` keeps working. A `--json` argument already
declared in `args` or `fixed` is passed through unchanged, and the probe is
skipped.

### Parsed help is advisory

`get_command_help` returns parsed `Usage:`, `Arguments:`, `Subcommands:`, and
`Flags:` sections, plus JSON help and, when no structured section was found,
raw help. Value-taking flags are marked with their kind — `(value)`,
`(string)`, `(integer)`, `(array)`, `(object)` — and boolean flags as
`(boolean)`.

The flag table is best-effort. `--help` is the schema, and the raw help is
authoritative: positional grammars such as kubectl's usage line are returned
as text rather than turned into a fake JSON schema.

`max_help_chars` caps JSON and raw help; parsed flags are not truncated. Set it
to `0` to omit JSON/raw help entirely.

In `mode="code"`, `get_command_help` returns a dict with `command`,
`description`, `usage`, `arguments`, `subcommands`, `flags`, `help`, and
`json_help`. Each parsed flag is `{names, type, default, choices, repeatable,
description}`.

## Running commands

`run_command` builds `[executable, *base_args, *command_path, *args]` and runs
it without a shell. Every argument is one argv item, so metacharacters are not
interpreted. The program can still do anything its own flags allow; use a
container or an MCP server for a real security boundary.

- `command` is a whitespace-separated path; quoting works. An unknown path
  raises a `ValueError` naming the known commands.
- `args` is a list of strings, one item per argument. A multi-word query is
  one item. Do not repeat the command path in `args`. Passing a string is
  accepted for direct calls and split like a shell line.
- `cwd`, `env`, and `timeout` configure the process. `timeout=0` disables the
  deadline.
- Non-zero exits are returned inline as `[exit code N]`, not raised, so the
  agent can read and recover.
- A usage failure appends the exact argv, an argv-list hint, and the command's
  parsed help, so the agent can fix one item and retry without another help
  call.
- `max_output_chars` caps the result. Truncation is head-only and appends a
  marker, so keep it off when the agent parses JSON. Prefer the CLI's own
  output-limit flags when available.

## Modes

| | `mode="search"` | `mode="code"` |
| --- | --- | --- |
| Agent | `dspy.ReAct` / `dspy.ReActV2` | `dspy.RLM` |
| `get_command_help` | text | `dict` |
| `search_commands` | text | `list[dict]` |
| `run_command` | text | text |
| Best for | A few lookups with small output | Several commands composed in Python |

`mode` only affects the discovery facade; explicit commands always return
text. The tool docstrings and signatures are written for DSPy's presentation,
so the model sees when to call help versus run.

## Explicit commands

If you know exactly which commands the agent needs, pass `commands=` and skip
`--help` discovery entirely. Each `Command` becomes one typed `dspy.Tool`:

```python
from dspy_agent_tools import Argument, CLI, Command, Option

gh = CLI(
    "gh",
    commands=[
        Command(
            command="pr list",
            name="pr_list",
            description="List pull requests in a repository.",
            args={
                "repo": Argument(help="owner/name"),
                "state": Option(
                    "--state",
                    default="open",
                    choices=("open", "closed", "merged"),
                ),
                "limit": Option("--limit", default=30, type=int),
                "label": Option("--label", repeatable=True),
                "draft": Option("--draft", is_flag=True),
            },
            fixed=("--json",),
        )
    ],
)
# gh == [gh_pr_list]; no search_commands/get_command_help/run_command.
```

`commands=` replaces discovery for that CLI group; the `--help` tools are not
created. Each tool name is `name_prefix + Command.name` (default prefix is the
executable name). Names must be unique case-insensitively. `mode` is ignored.

### `Command`

| Field | Meaning |
| --- | --- |
| `command` | Command path after the executable: `"pr list"`, `"db:migrate"` (one token), or `("pr", "list")`. |
| `name` | Tool name before `name_prefix`; must be a valid Python identifier. |
| `description` | Tool docstring the model sees. |
| `args` | Mapping of tool argument names to `Argument(...)` / `Option(...)`. |
| `fixed` | Always-present argv items, inserted after positionals and before flags, e.g. `("--json",)`. Must be a sequence of strings. |
| `ok_codes` | Frozenset of exit codes that do not get an `[exit code N]` notice. |

### `Argument` and `Option`

`Argument(...)` is a required positional; `Argument(default=...)` is optional.
Positional indices are assigned in `Command.args` order. Indices must be
contiguous from 0, required positionals must come before optional ones, and a
repeatable positional must be last.

`Option(...)` declares a flag:

- `Option("--limit", default=30, type=int)` becomes `--limit VALUE`.
- `Option(30, "--limit", type=int)` is the usecli form and means the same.
- `Option("--verbose", is_flag=True)` becomes `--verbose` when true and is
  omitted when false.
- `Option("...", "--state")` and `Option(..., "--state")` mark a required
  option (no default).
- `type=` is `str`, `int`, `float`, or `bool`. `choices=` requires `str`.
- `default=` makes an option optional. Boolean flags default to `False`;
  repeatable flags default to empty when no default is given; every other
  argument is required.
- `repeatable=True` repeats the flag for each value in a list. A repeatable
  default must be a collection such as a list or tuple.
- `choices=` adds an enum to the schema.
- Aliases are accepted for readability; the last long flag is the one used in
  argv.

Arguments are built as
`[executable, *base_args, *command_tokens, *positionals, *fixed, *flags]` and
run without a shell. The same `json_flag` probe applies to explicit commands.
Explicit commands do not probe `--help`, so they also work for hidden commands
and plugin commands that do not appear in help.

## Toolbox integration

`CLI` is already a discovery facade: pass its tools directly to the agent
instead of wrapping them in `Toolbox(mode="search")`. Use `Toolbox` when you
need to combine them with other tools under one catalog.

When the CLI also has an MCP server, prefer `MCP`: its schemas are exact
rather than inferred from help text.

## Safety

- `run_command` executes the configured executable with the caller's
  permissions. Its own flags can still touch files, the network, or other
  programs; only the executable name is constrained.
- Use a container, a restricted user, or an MCP server when the agent must not
  have the program's full authority.
- Discovery runs help with a fixed pager environment so it cannot block on a
  terminal, but it still executes the program's own help path.
