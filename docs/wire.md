# Wiring an agent

`Wire()` composes five optional capabilities around one agent:

| Capability | RLM | ReAct / ReActV2 |
| --- | --- | --- |
| Memory recall | `pre_iteration_hook` injects prompt context | recalled text goes into the injected guide |
| Memory tools | added to the REPL as `dspy.Tool`s | added to the tool list |
| Memory write-back | `post_iteration_hook`, iteration 1 | not automatic — call `memory.remember` yourself |
| Policy | `pre_execution_hook` rewrites denied code into a `PermissionError` | checked before every tool call |
| Offloading | `post_execution_hook` replaces an oversized result | wraps every tool result |
| Tracing | one span per iteration and per execution | one span per tool call |
| Session log | one row per iteration | one row per tool call |

```python
from dspy_agent_tools import Memory, Offloader, Session, Wire

Wire(
    agent,
    memory=Memory(store, user_id="user_123"),
    offload=Offloader(limit=20_000),
    session=Session("runs.db"),
    policy=deny_writes,
    trace=True,
)
```

`Wire` returns the same agent, so it chains with `Patch`.

## Requirements

| Feature | Extra |
| --- | --- |
| RLM hooks | `dspy-agent-tools[hooks]` (`dspy-rlm-hooks`) |
| OTLP spans | `dspy-agent-tools[otel]` (`opentelemetry-api`) |
| LanceDB memory | `dspy-agent-tools[memory]` (`dspy-lancedb-memory`) |

`ReAct` wiring, offloading, sessions, and policy need no extra. A memory store
is duck-typed, so any object with the three methods below works.

## Shortcuts

Every option is independent, and the flags mean "use the default":

```python
Wire(agent, policy=safe_defaults("."))  # one policy, one line
Wire(agent, offload=True, session=True)  # default Offloader and Session
Wire(agent, memory=store, user_id="u1")  # a bare store is wrapped for you
Wire(agent, policy=[workspace("."), deny_tools("web_search")])  # lists are ANDed
```

`Wire` is re-runnable: calling it again replaces the middleware rather than
stacking a second layer of logging, offloading, or policy. Tools added with
`Patch` after a `Wire` call are unwrapped until the next `Wire`.

## Memory

`Memory(store, user_id=...)` returns the three memory tools. On an RLM the
history is:

- `pre_iteration_hook` recalls once against `query=` or the call's inputs and
  injects the block as prompt context.
- `post_iteration_hook` extracts memories from the call's inputs on iteration
  1 (best-effort; a failure is logged).

The tools themselves are added to the agent, so generated code calls
`memory_recall(...)` / `memory_remember(...)` like any other tool. ReAct gets
the same tools, plus recalled text as injected guidance when `query=` is given.
The store contract, modes, scoping, and `MemoryTools` API are in
[docs/memory.md](memory.md).

## Offloading

`Offloader(limit=20_000)` replaces any result longer than `limit` characters
with:

```python
{
    "offloaded": True,
    "path": "/tmp/dspy-agent-tools/offload/bash_exec-<digest>.txt",
    "chars": 84211,
    "bytes": 84211,
    "head": "...first 2000 characters...",
    "tail": "...last 1000 characters...",
    "note": "84211 characters were saved to ...",
}
```

Files are content-addressed, so spilling the same value twice reuses one file.
Bytes and existing handles pass through unchanged. `head`/`tail` default to
half of `limit` when `limit` is small.

The model reads the spill with `read_file` or `bash_exec`. For ReAct, wrapping
is the only place a result can be transformed; for RLM,
`post_execution_hook` is the equivalent.

## Sessions

`Session(path=":memory:", key=None)` is an append-only SQLite log:

```python
session.record(kind="iteration", iteration=0, code="...", output="...")
session.artifact("/tmp/spill.txt", size=1200, note="bash_exec")
session.events(limit=20)
session.artifacts()
session.clear()
```

When `Wire` receives both `offload` and `session`, every spilled file is
recorded as an artifact automatically.

A session is a log, not a checkpoint. DSPy exposes no way to re-seed an RLM's
interpreter history, so `Wire` does not claim to resume a run: rows carry what
was generated and returned, for auditing or replay by hand.

## Tracing

`Wire` emits OpenTelemetry spans named `rlm.iteration`, `rlm.execute`, and
`execute_tool` with `gen_ai.*` attributes. Only the API is a dependency;
configure an OTLP exporter and SDK provider in the application and the spans
join the rest of the trace. Without `opentelemetry-api` every span is a no-op,
and `trace=False` skips them entirely.

## Policy

`Wire` accepts one policy or a list combined with conjunction. A policy is a
synchronous `(name, arguments) -> str | None` callable; a string denies the
call with that reason.

```python
from dspy_agent_tools import deny_tools, safe_defaults, workspace

Wire(agent, policy=safe_defaults("."))
Wire(agent, policy=workspace("src") & deny_tools("web_search"))
```

For a tool call, `name` is the tool name and `arguments` are its keyword
arguments. For generated RLM code, `name` is `"code"` and `arguments` holds
`code` and `iteration`; a denial rewrites the iteration into
`raise PermissionError(...)` so the model sees the reason and adapts.

The premade rules for the shipped tools — `workspace`, `protect_secrets`,
`destructive_shell`, `read_only`, `no_network`, `require_approval`, and the
rest — are documented in [docs/policies.md](policies.md).
