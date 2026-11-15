# Patching an existing agent

`Patch` adds tools to a DSPy agent that was already built and tells the model
how the new tool families relate to the ones it already had.

```python
import dspy
from dspy_agent_tools import CLI, Patch, Skills, bash_exec, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec], max_iters=8)

Patch(agent, tools=[read_file, Skills("./skills"), CLI("gh")], guide="Never commit.")
```

It works for `dspy.ReAct`, `dspy.ReActV2`, and `dspy.RLM`, returns the same
agent, and keeps its settings (iterations, callbacks, history, the compiled
flag, and for `dspy.RLM` its call budget, sub-LM, and interpreter factory).

## Why instructions, not input fields

A DSPy signature has three places a prompt can grow: **input fields**, the
**docstring** (`signature.instructions`, which every adapter sends as the
system message), and the **tool descriptions** the adapter renders. Guidance
belongs in the docstring; input fields are the wrong tool for it:

- `dspy.ReAct` copies the signature's input fields into the predictor it
  builds and `forward(**input_args)` needs them, so a new field becomes a new
  required keyword argument on every call.
- `dspy.RLM` is stricter: it rejects a call whose inputs were not supplied
  (`Missing required inputs: ['tool_guide']`) before defaults are considered,
  and binds each input as a REPL variable, so the field shows up in
  `variables_info` too.
- The field's text lands in the **user** message, wrapped in the adapter's
  field scaffolding (`[[ ## tool_guide ## ]]`), not in the system message. Only
  the field's description reaches the system prompt, as a mention of an input
  the caller never fills in.
- Eight fields (`skills`, `cli`, `mcp`, `browser`, `bash`, `web`, `files`,
  `commands`) would reserve those names on every signature, break on the
  first duplicate with a user input, and force every caller to pass text the
  agent already knows.

`tool.desc` stays the right home for per-tool wording: every adapter shows it
next to the tool, and `dspy.ReAct` already lists tool names, descriptions, and
argument schemas in its instructions. Repeating schemas would pay for the same
tokens twice, so the injected sections carry only what a single tool cannot
say about itself.

## What gets injected

One section per tool family the agent holds, in a fixed order, appended to the
existing instructions:

```markdown
Answer the question from the repository and the web.

The repository is checked out at ./repo. Never commit.

## Agent Skills

…guidance, execution rules, and the <available_skills> catalog…

## Shell — `bash_exec`, `bash_ripgrep`

These run host commands with the permissions of the agent process: one
command for installs, git, and tests, and the search tools to locate files or
text without building a pipeline. Read the output before reporting it; never
state a result you did not see and never invent command output.

## Files — `read_file`

Read a file before changing it: `read_file` returns its content and a snapshot
token, and passing that token makes a write made against a stale file fail
instead of overwriting a newer change. Never claim a file change you did not
make.
```

Families come from `dspy_agent_tools.patch.guide`, and a tool joins a family by its
module (`dspy_agent_tools.bash`, `.files`, `.search`, `.browser`, `.cli`,
`.mcp`, `.skills`, `.toolbox`) or by a name a foreign tool is likely to use,
such as `shell`, `run_command`, or `web_search`. The headings list the tools
that are actually on the agent, so a prefixed name (`name_prefix="skill_"`)
shows up as it is callable.

`dspy.RLM` additionally gets one environment section: its code runs in a REPL
that cannot spawn processes or open network connections, so an install or a
`curl` written in Python fails while the same work as a tool call succeeds.

## Options

```python
Patch(
    agent,
    tools=[read_file, bash_exec, Skills("./skills")],  # add
    drop="browser_open",  # revoke
    guide="Never edit vendor/.",  # your own policy, placed first
    describe=False,  # tools only, no family sections
)
```

| Option | Default | Effect |
| --- | --- | --- |
| `tools` | `()` | Tools to add: `dspy.Tool` objects, plain functions, or lists of either — the same keyword `dspy.ReAct` and `Toolbox` take. Adding accumulates, so two call sites can both extend one agent. |
| `drop` | `()` | Tools to remove, by name, tool, or function. Unknown names warn and are ignored, so a conditional drop stays idempotent; a name in both `tools` and `drop`, or an internal tool such as ReAct's `finish`, raises `ValueError`. |
| `guide` | `None` | Your text, placed before the family sections. Use it for project rules no tool family knows. Omitted, the agent keeps its last guide; `guide=""` clears it. |
| `describe` | `True` | Inject the family sections. `False` appends tools (and `guide`) only. |

Removing a tool removes its section too, since the region is recomputed from
the tools that remain:

```python
Patch(agent, drop="bash_exec")  # the ## Shell section goes with it
```

## The managed region

Everything `Patch` adds lives between two markers and is recomputed on every
call, so patching repeatedly converges instead of growing the system prompt:

```markdown
<!-- dspy-agent-tools:begin -->
Never commit.
## Files — `read_file`
…
<!-- dspy-agent-tools:end -->
```

- Editing `guide` replaces the old copy; it does not append a second one.
- The last guide carries over to later calls. `Patch(agent, tools=read_file,
  guide="Never commit.")` then `Patch(agent, tools=bash_exec)` keeps the rule
  and adds the shell section.
- A family you already documented — `## Shell` in your own docstring — is
  skipped, so hand-written policy wins. Source sections such as the skill
  catalog are always injected.
- Your own text outside the region is never touched.

## Rules

- The same function patched twice is one tool: `Patch` compares the tool
  object, or the function behind it, so `Patch(agent, tools=read_file)` is
  idempotent.
- Patching a second family adds its section and leaves the first alone; the
  region holds one copy of each.
- Patching after an optimizer has run warns and drops the few-shot demos on
  the inner predictor it rebuilds (the instructions survive). Patch before
  compiling, or recompile afterwards.
- An agent that already has a *different* tool with an incoming name raises
  `ValueError`; pass `name_prefix=...` to the factory that built that tool
  (`Skills(..., name_prefix="skill_")`, `CLI(..., name_prefix="gh_")`).
- `Patch` does not accept a `dspy.Predict` or a wrapper such as `dspy.Retry`
  around an agent; it needs the agent module itself (`TypeError`).

## Adding a family

Add a `Family` to `src/dspy_agent_tools/patch/guide.py` with its heading, modules, and
names, plus the policy text. A source can replace its family template by
exposing `family` and `section(tools)`, which receives the agent's full tool
list — that is how `Skills` injects the `<available_skills>` catalog and names
the host-command and URL-reading tools it actually has.
