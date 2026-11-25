# Durable execution

`Durable` turns an existing DSPy agent into a DBOS workflow. Predictor calls
and tool calls become checkpointed steps; a crashed process resumes from the
last completed operation instead of paying for the same LLM and tool calls
again.

## Install

```bash
uv add "dspy-agent-tools[dbos]"
```

Local development needs no configuration: DBOS defaults to a SQLite system
database in the working directory. Production should point at PostgreSQL:

```python
from dspy_agent_tools import Durable

Durable.configure(
    name="my-agents",
    database_url=os.environ["DATABASE_URL"],
)
```

Logging is quiet by default: DBOS INFO output and DSPy's direct-`forward`
warning are hidden. Pass `verbose=True` to see them:

```python
Durable.configure(name="my-agents", verbose=True)

# or per agent
agent = Durable(agent_module, name="researcher", verbose=True)
```

An explicit `log_level=` passed to `Durable.configure` takes precedence over
the quiet default.

## Captured inputs and outputs

Tool and predictor steps checkpoint what went in and what came out.
`run.steps()` loads the outputs by default:

```python
for step in run.steps():
    print(step["function_name"], step["output"])
```

A tool step's output contains the exact positional and keyword arguments plus
the returned value:

```python
{
    "__durable_tool_checkpoint__": True,
    "tool": "get_weather",
    "input": {"args": (), "kwargs": {"city": "Paris"}},
    "output": "sunny",
}
```

A predictor step contains its inputs, the parsed prediction fields, and the raw
LM responses captured while the prediction ran:

```python
{
    "__durable_llm_checkpoint__": True,
    "predictor": "react",
    "input": {"args": (), "kwargs": {"question": "...", "trajectory": ""}},
    "output": {"next_thought": "...", "next_tool_name": "..."},
    "output_type": "prediction",
    "usage": None,
    "llm": [["[[ ## next_thought ## ]] ..."]],
}
```

A `dspy.Prediction` is stored as plain fields for the DBOS viewer and rebuilt
on read, so step output stays readable instead of a pickled custom class.

The run itself is tracked as well: `run.started` carries the workflow input and
`run.completed` carries the final result. Events are written directly to the
DBOS stream with deterministic event IDs, so the DBOS step list contains only
actual agent work (predictor and tool steps) and no empty `DBOS.writeStream`
bookkeeping rows.

DBOS Argus does not render top-level workflow arguments on its own. The durable
layer also publishes `agent.input`, `agent.output`, and `agent.error` as
workflow events (and `function.*` for `DurableFunction`). They show up in
Argus's Events panel as decoded JSON, without adding a step to the timeline.

Values that DBOS cannot serialize are replaced by a readable summary instead
of failing the step. The persisted event stream carries the same inputs and
outputs in summarized form (`tool.started`, `tool.completed`, `llm.started`,
`llm.completed`), so a live UI can render progress without loading step rows.

## Two-line wrapping

```python
import dspy
from dspy_agent_tools import Durable, bash_fetch, web_search

Durable.configure(name="my-agents")  # once at startup
agent = Durable(
    dspy.ReAct("question -> answer", tools=[web_search, bash_fetch]),
    name="researcher",  # stable: recovery looks this workflow up by name
)

result = agent(question="Research the latest advances in RLMs")
print(result.answer)
```

`name=` must remain stable across deployments: it identifies the workflow
function that DBOS replays after a restart. Wrap each module once.

Start in the background and reconnect from any process:

```python
run = agent.start(question="What is new in DBOS?")
print(run.id, run.status())

# later, possibly another process
from dspy_agent_tools import Run

Run.get(run.id).result()
for event in Run.get(run.id).events():
    print(event)
```

Workflow arguments and results are serialized with cloudpickle, so
`dspy.Prediction` round-trips with its attributes intact.

The public surface is typed for type checkers: `Durable(module)` returns
`DurableAgent`, `Durable(fn)` and `@Durable.workflow` return
`DurableFunction`, `Durable.tool(fn)` and `Durable.step(fn)` preserve the
callable's signature, `Human(fn)` preserves its name/signature, and
`Worker(...)` returns a `WorkerHandle` with `start`/`enqueue`/`result`.

## Workflows and steps

`Durable.workflow` and `Durable.step` are the decorator spelling of
`Durable(fn)` and `Durable.tool(fn)`, mirroring `@DBOS.workflow` and
`@DBOS.step`:

```python
from dspy_agent_tools import Durable

Durable.configure(name="pipeline")


@Durable.step()
def fetch(url: str) -> str:
    return httpx.get(url).text


@Durable.workflow(name="pipeline")
def main(*, url: str) -> str:
    return summarize(fetch(url=url))


main(url="https://example.com")  # run and wait
run = main.start(url="https://example.com")  # background, returns Run
```

`Durable.workflow(...)` returns a `DurableFunction`, so `start`, `enqueue`,
and `schedule` work as they do for `Durable(fn)`. `Durable.step` checkpoints
the call's inputs and output when it runs inside a durable workflow; outside
one it calls the function normally. Workflow bodies take keyword arguments
only. Both decorators accept the same options as their call-form equivalents
(`name`, `retries`, `queue`, ...).

## Human approvals and questions

`Human(tool)` preserves the tool's name, signature, and docstring. The agent
pauses *before* the checkpointed tool step and waits for a persisted message:

```python
from dspy_agent_tools import Durable, Human, bash_exec, read_file

agent = Durable(
    dspy.RLM("task -> summary", tools=[read_file, Human(bash_exec), Human.ask]),
    name="coder",
)

run = agent.start(task="Improve the codebase")
for event in run.events():
    if isinstance(event, ApprovalRequest):
        event.approve()  # or event.reject(reason="...")
    elif isinstance(event, QuestionRequest):
        event.respond(input(event.question))
```

Out-of-band control does not need the agent object:

```python
pending = run.pending()
run.approve(pending[0].id)
run.reject(pending[0].id, reason="create a local commit instead")
run.respond(pending[0].id, answer="develop")

# global inbox across every waiting run
Human.pending()
```

Rejection is returned to the agent as an observation
(`{"status": "rejected", "reason": ...}`), so ReAct/RLM can choose another
action instead of failing the workflow.

Conditional approval:

```python
shell = Durable.tool(
    bash_exec,
    approval=lambda command, **_: command.startswith("git push"),
)
```

`Human.approve("Deploy?")` and `Human.ask("Which branch?")` also work as plain
Python calls: inside a workflow they persist; outside one they prompt the
terminal when it is interactive, and raise `HumanInputRequired` otherwise.

## Workers, queues, and schedules

```python
from dspy_agent_tools import Durable

Durable.queue("research", concurrency=10)

researcher = Durable(
    dspy.ReAct("question -> report", tools=[web_search]),
    name="researcher",
    queue="research",
)
run = researcher.enqueue(question="Compare vector databases")
run.result()

researcher.schedule(
    cron="0 8 * * *",
    inputs={"question": "What is new in AI research?"},
    name="daily-ai-research",
)
```

Plain functions become queued workers:

```python
@Durable.worker(queue="indexing", concurrency=5, retries=3)
def index_repository(repo: str) -> dict:
    return {"repo": repo}


index_repository(repo="thememium/dspy-agent-tools")  # run and wait
run = index_repository.enqueue(repo="thememium/dspy-agent-tools")
```

Worker bodies execute at the workflow level, so they can call `Human.ask`,
start other runs, or schedule work. Wrap individual side effects with
`Durable.tool` when they need their own checkpoint.

The remaining helpers are small on purpose:

```python
Durable.tools([read_file, bash_exec])  # checkpoint a collection
Durable.schedule(agent, cron="0 8 * * *", question="...")
Durable.sleep(3600)  # persisted inside a workflow
Durable.operation_id()  # stable idempotency key in a step
```

Queues and schedules are declared before `DBOS.launch()` / the first run in a
server. `Durable.launch()` and `Durable.shutdown()` make the lifecycle
explicit; standalone scripts launch automatically on first use. When another
DBOS application owns the process, use `Durable.configure(manage_runtime=False)`.

## Exposing workers to an LLM

`Durable.worker_tools([...])` returns an allowlisted tool set:
`list_workers`, `start_worker`, `worker_status`, `worker_result`,
`cancel_worker`, `wait_for_workers`, `schedule_worker`, and `list_schedules`.
The model can only start pre-registered workers; it cannot register arbitrary
code or unbounded schedules.

```python
agent = Durable(
    dspy.RLM(
        "task -> result",
        tools=[*Durable.worker_tools([researcher, index_repository])],
    ),
    name="orchestrator",
)
```

## Versioning, factories, and run control

Use `version=` to pin the workflow name, and `@Durable.agent` to build a fresh
module per run (important for agents that own browser or MCP sessions):

```python
@Durable.agent(name="coder", version="v3")
def make_coder():
    session = BrowserSession(state_path="./coder-state.json")
    return dspy.RLM("task -> summary", tools=[*Browser(session=session)])


# pin a workflow ID or deadline
run = make_coder.start(task="Review the PR", workflow_id="pr-42", timeout=900)

# queue controls
run = make_coder.enqueue(
    task="Review the PR", delay=60, priority=5, deduplication_id="pr-42"
)
```

`Run` exposes `result(timeout=...)`, `pause()`, `resume()`, `fork(...)`,
`retry()`, `steps()`, and `events()`. Pauses happen before the next predictor
or tool step; call `Durable.pause_point()` inside a plain workflow for an
explicit boundary. `Durable.list_runs(...)` and `Durable.list_schedules(...)`
cover the control plane. Gate human decisions with an authorizer:

```python
Durable.configure(authorizer=lambda action, run_id, tenant, request: tenant == "team-a")
```

Run workers with:

```bash
dspy-durable worker app:durable --queue research --processes 4
```

RLM sandbox sub-agent calls and each `llm_query_batched` prompt are
checkpointed individually. Toolbox discovery checkpoints the resolved catalog
tool, not the generic `call_tool` dispatcher. Give a tool a recovery hook with
`Durable.tool(..., recover=...)` or `Durable(..., recover=...)`; Browser and MCP
tools register their session reconnects automatically.

## What is durable, and what is not

- **LLM predictions** and **tool calls** are steps. `Durable.tool` can wrap a
  single function; agent instrumentation wraps every predictor and tool
  automatically.
- **ReAct, ReActV2, RLM**: the loop itself is deterministic replay. RLM
  regenerates the code from recorded predictions, re-executes it, and reuses
  recorded host tool and `llm_query` results. Sandbox sub-agent predictors and
  tools are instrumented individually, and each `llm_query_batched` prompt is
  its own step. Interpreter state is *not* snapshotted; replay requires the
  generated code to be deterministic.
- **Stateful tools**: Browser and MCP tools expose recovery hooks. Use
  `BrowserSession(state_path=...)` to checkpoint tab URLs, cookies, and
  local/session storage; DOM mutations and in-memory JavaScript state are not
  captured. MCP restarts a closed transport, but any state held privately by
  the server still has to be reconstructed by that server.
- **Side effects are not exactly-once.** DBOS records a completed step, but a
  crash between the external effect and the checkpoint can repeat it. Mark
  non-idempotent tools with `idempotent=False` and use
  `Durable.operation_id()` as an idempotency key:

  ```python
  @Durable.tool(idempotent=False)
  def create_order(customer_id: str) -> dict:
      return api.create_order(customer_id, idempotency_key=Durable.operation_id())
  ```

- Agent errors are recorded as `AgentExecutionError` with the original type
  and message, so `Run.result()` works even when the underlying exception is
  not picklable. Inspect `run.steps()` for the failing operation.
- `Run.cancel()` stops a run; `Run.resume()` resumes an interrupted one.
