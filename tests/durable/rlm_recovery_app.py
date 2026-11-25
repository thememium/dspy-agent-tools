"""Subprocess fixture for the real RLM crash-recovery test.

Usage: python rlm_recovery_app.py first|recover

The first process runs a real ``dspy.RLM`` in a Deno/Pyodide interpreter. It
records a seed through a host tool, then dies inside a second host tool. The
recovery process re-executes the generated Python to rebuild interpreter
variables, reuses the recorded action and seed-tool steps, and finishes.
"""

from __future__ import annotations

import os
import pathlib
import sys

import dspy
from dbos import SetWorkflowID
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import Durable, Run

HERE = pathlib.Path(os.environ["DURABLE_DIR"])
APP = os.environ["DURABLE_APP"]
RUN_ID = f"{APP}-rlm"
MODE = sys.argv[1]
LM_CALLS = HERE / "lm_calls.txt"
SEED_CALLS = HERE / "seed_calls.txt"
CRASH_CALLS = HERE / "crash_calls.txt"
CRASHED = HERE / "crashed"

# Keyed by the iteration field so replay generates the same Python again.
ANSWERS = {
    "1/4": {
        "reasoning": "seed",
        "code": (
            'q = "subagent" + "-question"\n'
            'sub = dspy.Predict("question -> answer")\n'
            "answer = sub(question=q)\n"
            "seed = seed_tool()\n"
            "print(answer.answer, seed)"
        ),
    },
    "subagent-question": {"answer": "sub-answer"},
    "2/4": {"reasoning": "crash", "code": "print(seed)\ncrash_tool(seed)"},
    "3/4": {
        "reasoning": "submit",
        "code": "print(seed)\nSUBMIT(answer=seed)",
    },
}


class CountingLM(DummyLM):
    def __call__(self, *args, **kwargs):
        with LM_CALLS.open("a") as handle:
            handle.write("x\n")
        return super().__call__(*args, **kwargs)


def seed_tool() -> str:
    """Return a deterministic seed value."""
    with SEED_CALLS.open("a") as handle:
        handle.write("seed\n")
    return "seed-value"


def crash_tool(value: str) -> str:
    """Crash the first process after the seed has been checkpointed."""
    with CRASH_CALLS.open("a") as handle:
        handle.write(f"crash:{value}\n")
    if MODE == "first" and not CRASHED.exists():
        CRASHED.write_text("1")
        os._exit(17)
    return "ok"


dspy.configure(lm=CountingLM(ANSWERS))
Durable.configure(
    name=APP,
    database_url=f"sqlite:///{HERE / 'db.sqlite'}",
    log_level="WARNING",
)
agent = Durable(
    dspy.RLM(
        "question -> answer",
        tools=[seed_tool, crash_tool],
        max_iters=4,
    ),
    name="rlm-recoverer",
)

if MODE == "first":
    with SetWorkflowID(RUN_ID):
        run = agent.start(question="?")
    print(run.result())
else:
    Durable.launch()
    print(Run.get(RUN_ID).result().answer)
Durable.shutdown()
