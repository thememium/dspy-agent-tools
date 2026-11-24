"""Deterministic LM and polling helpers for durable tests."""

from __future__ import annotations

import time
from typing import Any

from dspy.utils.dummies import DummyLM

from dspy_agent_tools import Run


class KeyedLM(DummyLM):
    """DummyLM whose answers depend on the prompt, not the call order.

    Replay reissues the same prompt after a crash, so a positional script would
    make recovered trajectories diverge. Keyed answers keep the trajectory
    deterministic across replay.
    """

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        super().__init__(answers)
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return super().__call__(*args, **kwargs)


def wait_for_pending(run: Run, timeout: float = 15.0) -> list[Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = run.pending()
        if pending:
            return pending
        if not run.is_active():
            return []
        time.sleep(0.05)
    raise AssertionError(f"run {run.id} did not ask for human input")


def wait_for_status(run: Run, status: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run.status() == status:
            return
        time.sleep(0.05)
    raise AssertionError(f"run {run.id} did not reach {status}")
