"""Subprocess fixture for the crash-recovery test.

Usage: python recovery_app.py first|recover
The first process dies inside crash_tool; the second recovers the workflow.
"""

from __future__ import annotations

import os
import pathlib
import sys

import dspy
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import Durable, Run

HERE = pathlib.Path(os.environ["DURABLE_DIR"])
APP = os.environ["DURABLE_APP"]
MODE = sys.argv[1]
LM_CALLS = HERE / "lm_calls.txt"
TOOL_CALLS = HERE / "tool_calls.txt"
WID = HERE / "wid.txt"
CRASHED = HERE / "crashed"

ANSWERS = {
    "[[ ## answer ## ]]": {"reasoning": "looked it up", "answer": "Sunny in Paris"},
    "observation_1": {
        "next_thought": "done",
        "next_tool_name": "finish",
        "next_tool_args": {},
    },
    "observation_0": {
        "next_thought": "crash",
        "next_tool_name": "crash_tool",
        "next_tool_args": {},
    },
    "next_tool_name": {
        "next_thought": "weather",
        "next_tool_name": "get_weather",
        "next_tool_args": {"city": "Paris"},
    },
}


class CountingLM(DummyLM):
    def __call__(self, *args, **kwargs):
        with LM_CALLS.open("a") as handle:
            handle.write("x\n")
        return super().__call__(*args, **kwargs)


def get_weather(city: str) -> str:
    """Get the weather."""
    with TOOL_CALLS.open("a") as handle:
        handle.write(f"weather:{city}\n")
    return "sunny"


def crash_tool() -> str:
    """Crash the first process inside this tool."""
    with TOOL_CALLS.open("a") as handle:
        handle.write("crash\n")
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
    dspy.ReAct("question -> answer", tools=[get_weather, crash_tool]),  # ty: ignore[invalid-argument-type]
    name="recoverer",
)

if MODE == "first":
    run = agent.start(question="weather and crash?")
    WID.write_text(run.id)
    print(run.result())
else:
    Durable.launch()
    print(Run.get(WID.read_text().strip()).result().answer)
Durable.shutdown()
