# Agent Skills

`dspy_agent_tools.skills` loads [Agent Skills](https://agentskills.io/specification)
folders — the same `SKILL.md` folders Claude Code, Codex, and other clients use
— and exposes them as progressive-disclosure tools. Only a skill's name,
description, and declared `allowed-tools` enter the model context at startup;
its body loads when the agent activates it.

## Quick start

```python
import dspy
from dspy_agent_tools import Skills, bash_exec

skills = Skills("~/.agents/skills", ".agents/skills")

agent = dspy.ReAct("question -> answer", tools=skills + [bash_exec])
```

`Skills` returns a `SkillTools` list, so it can be passed as `tools=`,
concatenated with other tools, or unpacked. It contains:

| Tool | Purpose |
| --- | --- |
| `list_skills()` | Names, descriptions, declared `allowed-tools`, and bundled files for every skill. |
| `activate_skill(skill)` | The skill's full `SKILL.md` body, its bundled file list, and its declared `allowed-tools` (code mode also returns `license`, `compatibility`, and `metadata`). |
| `read_skill_resource(skill, path)` | One file from `references/` or `assets/`, relative to the skill folder. |
| `run_skill_script(skill, script, args=[])` | One file from `scripts/`, run in the sandbox below. |

`dspy.RLM` gets the same tools with structured results:

```python
worker = dspy.RLM("task -> report", tools=Skills("./skills", mode="code"))
```

## Skill layout

A skill is a directory with a `SKILL.md` and optional `scripts/`,
`references/`, and `assets/` folders:

```text
sqlite-tools/
├── SKILL.md              # name, description, metadata + instructions
├── scripts/query.py      # executable code the agent can run
├── references/schema.md  # documentation loaded on demand
└── assets/report.html    # templates and data
```

`SKILL.md` starts with YAML frontmatter:

```markdown
---
name: sqlite-tools
description: Query and inspect SQLite databases with bundled scripts.
allowed-tools: bash_exec read_file
metadata:
  author: example
---

# SQLite tools

Use `scripts/query.py` to run a read-only query ...
```

The body is Markdown instructions. `allowed-tools`, `license`,
`compatibility`, and `metadata` are optional. Activation shows the body, the
bundled file list, and `allowed-tools`; `mode="code"` also returns `license`,
`compatibility`, and `metadata`.

## Discovery and validation

`Skills(*roots, ...)` scans each root:

- A root that is itself a skill (contains `SKILL.md`) is accepted directly.
- Otherwise, every immediate subdirectory is a candidate.
- `SKILL.md` and `skill.md` are both recognized.
- Files under `scripts/`, `references/`, and `assets/` are listed recursively.
  Hidden files, `__pycache__`, and symlinks that escape the skill folder are
  skipped.

Validation rules:

- `name` must be a non-empty lowercase string of letters, digits, and hyphens,
  at most 64 characters, with no leading/trailing hyphen and no `--`. It must
  match the directory name.
- `description` must be non-empty and at most 1024 characters.
- `compatibility` is at most 500 characters.
- `metadata` must be a mapping; `allowed-tools`, `license`, and `compatibility`
  must be strings.
- Skill names must be unique across all roots.

An invalid skill is skipped, logged, and recorded on `skills.errors`; the
remaining skills still load. Pass `strict=True` to raise `ValueError` listing
every invalid skill instead. `skills.skills` holds the parsed `Skill` objects.

## Modes

`mode="search"` (default) returns text for `dspy.ReAct` and `dspy.ReActV2`.
`mode="code"` returns structured values for `dspy.RLM`:

| Tool | `mode="search"` | `mode="code"` |
| --- | --- | --- |
| `list_skills` | Formatted text. | `list[dict]` with `name`, `description`, `allowed_tools`, `scripts`, `references`, `assets`. |
| `activate_skill` | Formatted text. | `dict` with `name`, `description`, `path`, `instructions`, `allowed_tools`, `scripts`, `references`, `assets`, `license`, `compatibility`, `metadata`. |
| `read_skill_resource` | Text with a path header. | `dict` with `path`, `content`, `truncated`. |
| `run_skill_script` | Rendered output text. | `dict` with `script`, `exit_code`, `timed_out`, `elapsed`, `output`, `truncated`, `sandbox`. |

Under `mode="code"` the model also runs in a REPL, so the injected guidance
says that only tool calls reach the machine: shell commands written in the
REPL cannot spawn processes or open network connections.

Tool errors are returned in-band (`Error: ...` text, or an `error` key in the
dict), so the agent can read and recover. Construction errors — no roots, a
bad `mode`, an invalid `name_prefix`, a forced sandbox that is missing — raise.

## Name prefixes and scripts

- `name_prefix="skill_"` avoids collisions with other tools. It must produce
  valid Python identifiers for every tool name, which `dspy.RLM` requires;
  `"skill_"` is accepted and `"sk-"` raises `ValueError`.
- `scripts=False` omits `run_skill_script`, never executes skill code, and
  skips sandbox setup. The injected guidance then tells the model that scripts
  cannot be run.
- `max_resource_chars` caps one `read_skill_resource` result (default 50,000;
  `0` keeps everything).
- `max_catalog_chars` caps the `<available_skills>` block injected by
  `patch()` (default 8,000; `0` keeps every skill). Skills that do not fit are
  omitted with a note.

Binary files and non-UTF-8 files are refused with a message pointing at a file
tool, rather than decoded into garbage.

## Patching an existing agent

`tools=skills` is enough on its own. `patch(agent, Skills("./skills"))`
appends the catalog and tools to an agent that was already built:

```python
from dspy_agent_tools import Skills, bash_exec, patch, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec, read_file])
patch(agent, Skills("./skills"))
```

There is no `Skills.patch` method: [patch](patch.md) is the one way to extend a
built agent, and a skill list is just another tool source. `patch` rebuilds
the inner predictor so it works for `dspy.ReAct`, `dspy.ReActV2`, and
`dspy.RLM`:

- The agent's signature gets the skill guidance, the execution rules, and the
  `<available_skills>` catalog appended to its instructions.
- The four skill tools are added to the tool list. The rebuild filters out
  ReAct's internal `finish` tool (or ReActV2's `submit` tool) before adding
  them, so the module recreates its own instead of duplicating it. RLM keeps
  its iteration, output, verbosity, sub-LM, and interpreter settings.
- Module state such as callbacks, history, and the compiled flag is copied
  onto the patched instance.
- It returns the agent for chaining: `agent = patch(agent, skills)`.

Add other tools in the same call —
`patch(agent, Skills("./skills"), read_file)` — or add policy of your own with
`guide=`.

The injected block carries execution rules, not just the catalog. Only
`run_skill_script` can start a bundled file, and a skill's prose may call for
host commands (`npx skills find ...`, `gh skill search ...`) or a page to
read. `patch` names whichever it finds on the agent:

- Host-command tools: tools from the shell family —
  `dspy_agent_tools.bash`, `dspy_agent_tools.cli`, and foreign tools named
  like a shell (`shell`, `run_command`).
- URL-reading tools: tools from the web or browser family —
  `dspy_agent_tools.search`, `dspy_agent_tools.browser`, such as
  `web_search`.

When the agent holds neither, the model is told to report the exact command
instead of running it, and never to invent command output, search results, or
install counts:

```python
# No shell tool: the model reports `npx skills find "git review"` for you to run.
agent = dspy.RLM("question -> answer")
patch(agent, Skills("./skills", mode="code"))

# With a CLI tool: the model runs the command for real and quotes the output.
from dspy_agent_tools import CLI

agent = dspy.ReAct("question -> answer", tools=list(CLI("gh")))
patch(agent, Skills("./skills"))
```

Rules:

- `patch` is idempotent: patching twice with the same `Skills` instance
  changes nothing, and the catalog appears once in the instructions.
- Patching an agent that already received these tools through `tools=` only
  adds the catalog.
- It raises `ValueError` when the agent already has a *different* tool with one
  of the skill tool names; pass `name_prefix="skill_"` to resolve the
  collision. A second `Skills()` instance collides exactly this way, so put
  every folder into one `Skills(...)` call.
- A skill whose instructions call for a shell command is followed with the
  shell or command tools on the agent; without one, the model names the
  command for the user to run.
- It raises `TypeError` for module types other than `dspy.ReAct`,
  `dspy.ReActV2`, and `dspy.RLM`.
- An agent accepts one catalog, so put every folder into one `Skills(...)`
  call.

The public helpers behind the injected block are also available:

```python
skills.catalog()  # the <available_skills> XML block
skills.section(tools=())  # guidance + execution rules + catalog
skills.skills  # parsed Skill objects
skills.errors  # skipped directories
```

## Sandboxed scripts

`run_skill_script` runs one file from the skill's `scripts/` folder. Scripts
are argv-only (no shell), use an allowlisted interpreter, must resolve inside
the skill folder, and run with a *replacement* environment: no ambient
secrets, and `HOME`/`TMPDIR` point at a throwaway directory. The process runs
in its own group, so a timeout kills any children it spawned.

Defaults:

- Interpreter allowlist: `python`, `bash`, `sh`, `node`, `deno`, `ruby`,
  `perl`. The interpreter is chosen from the script suffix or its shebang.
- Timeout: 30 seconds.
- `allow_network=False`, `allow_write=False`.
- `max_output_chars=0` (unlimited). Truncation appends a marker.
- `args` must be a list of strings; a bare string is rejected.

Where an OS sandbox is installed, it wraps the run:

| Platform | Backend | Enforcement |
| --- | --- | --- |
| Linux | `bwrap` (preferred) | All namespaces unshared, host mounted read-only, tmpfs `/tmp`, network unshared unless allowed. |
| Linux | `firejail` (fallback) | Private `/tmp`, read-only root unless writes are allowed, `--net=none` unless network is allowed. |
| macOS | `sandbox-exec` | Writes denied except the throwaway temp directory (plus the skill folder when writes are allowed); `network*` denied unless allowed. |
| any | none | Process confinement only: allowlist, path guard, scrubbed environment, timeout. |

`sandbox` selects the behavior:

- `"auto"` (default) uses an installed OS backend and falls back to process
  confinement with a log warning.
- `"required"` refuses the fallback and fails at construction when no backend
  is installed.
- A backend name (`"bwrap"`, `"firejail"`, `"sandbox-exec"`) forces that
  backend and fails at construction when it is missing or unsupported on the
  platform.
- `"off"` skips the OS layer.

`allow_write=True` permits writes: bwrap and sandbox-exec open the skill
folder, while firejail drops its read-only root and follows filesystem
permissions. The throwaway temp directory is always writable.
`allow_network=True` lets the OS backend share the network. Use
`sandbox="required"` when skill scripts are not trusted: process confinement
is not a security boundary, so a script can still touch anything the agent
process can.

```python
Skills(
    "./skills",
    timeout=60,
    allow_network=True,
    allow_write=True,
    sandbox="required",
    interpreters=("python", "bash"),
    max_output_chars=20_000,
)
```

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `*roots` | required | Folders that house the skills. At least one is required. |
| `mode` | `"search"` | `"search"` for text tools, `"code"` for structured results. |
| `name_prefix` | `""` | Prepended to every tool name; must form valid Python identifiers. |
| `strict` | `False` | Raise listing invalid skills instead of skipping them. |
| `scripts` | `True` | Include `run_skill_script`; `False` disables script execution. |
| `sandbox` | `"auto"` | OS sandbox selection: `auto`, `required`, `off`, or a backend name. |
| `timeout` | `30` | Script deadline in seconds; the whole process group is killed. |
| `allow_network` | `False` | Let scripts reach the network. |
| `allow_write` | `False` | Permit writes; bwrap/sandbox-exec open the skill folder, firejail drops its read-only root. |
| `interpreters` | python, bash, sh, node, deno, ruby, perl | Interpreter families scripts may use. |
| `env` | `None` | Extra environment variables, merged into the replacement environment. |
| `max_output_chars` | `0` | Cap one script result; `0` keeps everything. |
| `max_resource_chars` | `50_000` | Cap one `read_skill_resource` result; `0` keeps everything. |
| `max_catalog_chars` | `8_000` | Cap the injected catalog; `0` keeps every skill. |

## Safety

- Skill scripts are untrusted code chosen by the model. Run them under
  `sandbox="required"` when they are not yours, and keep `allow_network` and
  `allow_write` off unless the skill needs them.
- `activate_skill` and `read_skill_resource` only read text from the skill
  folder; `../` and absolute paths are rejected after resolution.
- The catalog is data, not authority: a skill can describe commands the agent
  cannot run. `patch` tells the model to report those commands instead of
  inventing output.
