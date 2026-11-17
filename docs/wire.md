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

## Memory

```python
class Store(Protocol):
    def create_memory(
        self, *, user_id, content, session_id="", memory_type="semantic"
    ): ...
    def create_memories(self, *, user_id, contents, session_id="", extract=True): ...
    def search_memories(self, *, user_id, query, session_id=None, limit=5): ...
    def delete_memory(self, *, memory_id): ...  # optional
```

`Memory(store, user_id=...)` returns three tools:

- `memory_recall(query, limit=5)` — text for ReAct, `list[dict]` for `mode="code"`.
- `memory_remember(content, memory_type="semantic")`
- `memory_forget(memory_id)` — omitted when the store has no `delete_memory`.

`MemoryTools.recall()`, `.remember()`, `.remember_turn()`, and `.format()` are
plain Python, used by the hooks and available to your code.

For an RLM, `Wire` adds the memory family as tools, so generated code calls
`memory_recall(...)` directly. Recall happens once per run against the call's
inputs (or `query=`), and iteration 1 extracts new memories from those inputs.

Write-back is best-effort: a store or extraction failure is logged and the
agent continues. Pass `write_back=False` to disable it.

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

A policy is a synchronous callable:

```python
def policy(name: str, arguments: dict) -> str | None:
    if name == "bash_exec" and "rm -rf" in arguments.get("command", ""):
        return "destructive commands are not allowed"
    return None
```

For tool calls, `name` is the tool name and `arguments` are the keyword
arguments. For generated RLM code, `name` is `"code"` and `arguments` holds
`code` and `iteration`.

A denial on a tool call raises `PermissionError`. A denial on generated code
rewrites the iteration into `raise PermissionError(...)`, so the model sees the
reason in its REPL history and can adapt instead of crashing the run.
