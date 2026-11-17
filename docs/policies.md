# Policies

A policy decides whether one action may run. It is a callable that receives the
tool name and keyword arguments and returns `None` to allow or a string to deny:

```python
def no_writes(name: str, arguments: dict) -> str | None:
    if name in {"write_file", "edit_file", "delete_file"}:
        return "this agent is read-only"
    return None
```

Returning a string raises `PermissionError` with that text on a tool call. For
RLM-generated code the denial is rewritten into `raise PermissionError(...)`, so
the model reads the reason in its REPL history and adapts instead of crashing.

`Wire(agent, policy=...)` and `wrap_tools(..., policy=...)` accept one policy, a
list, or a `Policy`; lists are combined with conjunction.

## Composition

```python
from dspy_agent_tools import deny_tools, workspace

policy = workspace("src") & deny_tools("web_search")
```

`&` means "all must allow": the first denial wins and its reason is used.
`Policy.all(a, b, c)` does the same without operators, and `None` entries are
skipped, so a conditional rule can be `None` when disabled.

```python
from dspy_agent_tools import Policy

policy = Policy.all(workspace("."), None, deny_tools("bash_exec"))
```

## Build a preset

The factories below target the tools this package ships, but every one takes
`tools=` so the same rule covers a renamed, prefixed, or third-party tool.
Names are fnmatch patterns: `browser_*` blocks the browser family.

| Factory | Denies | Default targets |
| --- | --- | --- |
| `allow_tools(*names)` | every call not named | all calls |
| `deny_tools(*names)` | the named calls | all calls when no names |
| `deny_patterns(*regex, keys=, tools=)` | a string argument matching a regex | all calls unless `tools=` |
| `deny_shell(*regex, tools=)` | a `command` matching a regex | `bash_exec` |
| `destructive_shell(*extra)` | `rm -rf`, `sudo`, `mkfs`, `dd of=`, `shutdown`, fork bombs, `chmod 777`, `curl \| sh`, `>/dev/sd*`, `git push --force` | `bash_exec` |
| `deny_code(*regex)` | generated Python matching a regex | `"code"` (RLM) |
| `workspace(root, tools=)` | any path resolving outside `root` | `read_file`, `write_file`, `edit_file`, `delete_file` |
| `protect(*globs, tools=)` | a path matching a glob | the four file tools |
| `protect_secrets(*extra)` | `.env`, `*.pem`, `*.key`, `id_rsa*`, `.ssh/*`, `.aws/*`, `**/secrets/**`, `**/credentials*`, and more | the four file tools |
| `deny_urls(*hosts, schemes=, tools=)` | a listed host (and its subdomains) or scheme | all calls; every string argument is scanned for URLs |
| `require_approval(*tools, ask=)` | a call the callback rejects | all calls when no names |

The tool sets are exported if you want to extend one:

```python
from dspy_agent_tools import FILE_TOOLS, SHELL_TOOLS, deny_shell

policy = deny_shell(tools=SHELL_TOOLS | {"terminal"})
```

### Tools

```python
from dspy_agent_tools import allow_tools, deny_tools

Wire(agent, policy=deny_tools("browser_*", "bash_exec"))
Wire(agent, policy=allow_tools("read_file", "bash_ripgrep", "bash_find"))
```

`allow_tools` is a closed list: anything not named is denied. For an RLM,
include `"code"` or every generated block is denied.

### Shell

```python
from dspy_agent_tools import deny_shell, destructive_shell

Wire(agent, policy=destructive_shell())  # block known-dangerous commands
Wire(agent, policy=deny_shell())  # block shell entirely
Wire(agent, policy=deny_shell(r"\bnpm\s+publish\b", r"\bdocker\s+rm\b"))
```

With no patterns, `deny_shell()` denies every shell call. Patterns are
case-insensitive regexes searched against the `command` argument.

### Files

```python
from dspy_agent_tools import protect_secrets, workspace

Wire(agent, policy=workspace("/srv/app") & protect_secrets())
```

- Relative paths resolve against `root`, so `path="notes.txt"` means
  `<root>/notes.txt`.
- Absolute paths and symlinks are resolved before the check: `../`, `/etc/...`,
  and a symlink pointing out of the tree are all denied.
- `protect` matches the whole path, the basename, and shell-style globs, so
  `.env`, `*.pem`, `.ssh/*`, and `**/secrets/**` all work.

`workspace` covers the file tools. Pass `tools=FILE_TOOLS | SHELL_TOOLS` to
check a shell call's `cwd`, but that does not stop the command from changing
directory — use an OS sandbox for that.

### Network

```python
from dspy_agent_tools import deny_urls

Wire(agent, policy=deny_urls("internal.corp", schemes=("file", "ftp")))
```

Every string argument is scanned for URLs, so the rule also catches a URL placed
in a shell command or a browser action. A host blocks itself and its subdomains:
`example.com` also blocks `api.example.com`. Malformed URLs are denied rather
than guessed at. `deny_urls()` with no hosts or schemes raises `ValueError`.

### Generated code

```python
from dspy_agent_tools import deny_code

Wire(agent, policy=deny_code(r"\bsubprocess\b", r"\bshutil\.rmtree\b"))
```

`deny_code()` with no patterns denies all generated code, which makes an RLM
unusable; use a pattern list there.

### Approval

```python
from dspy_agent_tools import require_approval


def ask(name: str, arguments: dict) -> bool | str:
    answer = input(f"run {name} {arguments}? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


Wire(agent, policy=require_approval("write_file", "bash_exec", ask=ask))
```

`ask` returns `True` to allow, a non-empty string to deny with that text, or
anything else (including `False`) to deny with the default reason. It runs
synchronously in the call path, so keep it fast; for a web UI, push the request
to a queue and block on the answer.

## Bundles

| Bundle | Contents |
| --- | --- |
| `read_only()` | deny write tools, shell, and generated code |
| `no_network()` | deny `bash_curl`, `bash_fetch`, `web_search`, `web_news`, and the browser family |
| `safe_defaults(root=".")` | `workspace(root) & protect_secrets() & destructive_shell()` |

```python
from dspy_agent_tools import Wire, read_only, safe_defaults

Wire(inspector, policy=read_only())
Wire(coder, policy=safe_defaults("."))
```

On an RLM, the file and URL rules see tool calls, not the generated Python.
Add `deny_code(...)` for code-level rules, and rely on the interpreter sandbox
as the real boundary.

`read_only()` denies `"code"`, so it cannot be used with an RLM. Compose the
parts you want instead:

```python
Wire(rlm, policy=workspace(".") & deny_tools("write_file", "edit_file", "delete_file"))
```

## Custom rules

A `Policy` is optional. Any callable works, and a class with `__call__` is fine
when a rule needs configuration:

```python
from dspy_agent_tools import Policy


def max_command_length(name: str, arguments: dict) -> str | None:
    command = arguments.get("command", "")
    if name == "bash_exec" and len(command) > 500:
        return "command is too long"
    return None


policy = Policy(max_command_length, name="max_command_length")
```

Check a policy before wiring it:

```python
policy("write_file", {"path": "../outside"})  # -> a reason, or None
```

## Limits

- `bash_exec` can do anything the agent process can do. Regex denylists are a
  speed bump, not a security boundary; run untrusted agents in a container.
- Path and URL rules inspect tool arguments. A shell command can still read a
  protected file or reach a blocked host, because the rule never sees the
  syscalls.
- A policy applies when `Wire` or `wrap_tools` builds the middleware. A tool
  added later with `Patch` is not wrapped; call `Wire` again after adding it.
  Re-wiring replaces the middleware instead of stacking another layer.
- Policies run synchronously in the tool-call path and in `pre_execution_hook`.
  Keep them offline and fast.
