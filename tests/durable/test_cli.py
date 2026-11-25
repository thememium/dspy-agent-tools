"""Tests for the dspy-durable worker CLI."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dspy_agent_tools.durable import cli


def test_load_rejects_bad_specs():
    with pytest.raises(SystemExit, match="module:attribute"):
        cli._load("json")
    with pytest.raises(SystemExit, match="has no attribute"):
        cli._load("json:missing")


def test_load_returns_attribute():
    assert cli._load("json:dumps").__name__ == "dumps"


def test_run_launches_and_shuts_down(monkeypatch):
    calls: list[str] = []
    queues = {"research": {}}

    class Runtime:
        def __init__(self) -> None:
            self.queues = queues

    monkeypatch.setattr(cli, "_load", lambda spec: calls.append(spec))
    monkeypatch.setattr(cli.Durable, "launch", lambda: calls.append("launch"))
    monkeypatch.setattr(
        cli.Durable, "shutdown", lambda **kwargs: calls.append("shutdown")
    )
    monkeypatch.setattr(
        cli.threading, "Event", lambda: SimpleNamespace(wait=lambda: None)
    )
    monkeypatch.setattr("dspy_agent_tools.durable.runtime.runtime", lambda: Runtime())
    cli._run("app:durable", [])
    assert calls == ["app:durable", "launch", "shutdown"]


def test_run_rejects_unknown_queue(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load", lambda spec: None)
    monkeypatch.setattr(cli.Durable, "launch", lambda: None)
    monkeypatch.setattr(
        cli.Durable, "shutdown", lambda **kwargs: calls.append("shutdown")
    )
    monkeypatch.setattr(
        "dspy_agent_tools.durable.runtime.runtime",
        lambda: SimpleNamespace(queues={}),
    )
    with pytest.raises(SystemExit, match="unknown queue"):
        cli._run("app:durable", ["missing"])
    assert calls == ["shutdown"]


def test_run_swallows_keyboard_interrupt(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load", lambda spec: None)
    monkeypatch.setattr(cli.Durable, "launch", lambda: None)
    monkeypatch.setattr(
        cli.Durable, "shutdown", lambda **kwargs: calls.append("shutdown")
    )

    def wait() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.threading, "Event", lambda: SimpleNamespace(wait=wait))
    monkeypatch.setattr(
        "dspy_agent_tools.durable.runtime.runtime",
        lambda: SimpleNamespace(queues={}),
    )
    cli._run("app:durable", [])
    assert calls == ["shutdown"]


def test_main_terminates_children_on_interrupt(monkeypatch):
    terminated: list[bool] = []

    class Process:
        def start(self) -> None:
            return None

        def join(self) -> None:
            raise KeyboardInterrupt

        def terminate(self) -> None:
            terminated.append(True)

    class Context:
        def Process(self, **kwargs: Any) -> Any:
            return Process()

    monkeypatch.setattr(cli.multiprocessing, "get_context", lambda name: Context())
    assert cli.main(["worker", "app:durable", "--processes", "2"]) == 0
    assert terminated == [True, True]


def test_child_swallows_keyboard_interrupt(monkeypatch):
    def run(spec: str, queues: list[str]) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", run)
    cli._child("app:durable", [])


def test_main_single_process(monkeypatch):
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "_run", lambda spec, queues: calls.append((spec, queues)))
    assert cli.main(["worker", "app:durable", "--queue", "q"]) == 0
    assert calls == [("app:durable", ["q"])]


def test_main_rejects_zero_processes():
    with pytest.raises(SystemExit):
        cli.main(["worker", "app:durable", "--processes", "0"])


def test_main_spawns_processes(monkeypatch):
    class Process:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.started = False
            self.joined = False

        def start(self) -> None:
            self.started = True

        def join(self) -> None:
            self.joined = True

        def terminate(self) -> None:
            return None

    processes: list[Process] = []

    class Context:
        def Process(self, **kwargs: Any) -> Any:
            process = Process(**kwargs)
            processes.append(process)
            return process

    monkeypatch.setattr(cli.multiprocessing, "get_context", lambda name: Context())
    assert cli.main(["worker", "app:durable", "--processes", "2"]) == 0
    assert len(processes) == 2
    assert all(process.started and process.joined for process in processes)
