"""Tests for dspy_agent_tools.wire.session."""

from __future__ import annotations

from pathlib import Path

from dspy_agent_tools import Session
from dspy_agent_tools.wire.session import OUTPUT_LIMIT


class TestSession:
    def test_records_events_in_order(self):
        session = Session()

        session.record(kind="iteration", iteration=1, code="x = 1", output="1")
        session.record(kind="tool", output="done", data={"tool": "bash_ls"})

        events = session.events()
        assert [event["kind"] for event in events] == ["iteration", "tool"]
        assert events[0]["iteration"] == 1
        assert events[0]["code"] == "x = 1"
        assert events[1]["data"] == {"tool": "bash_ls"}
        assert all(event["created_at"] for event in events)

    def test_limit_returns_most_recent_in_order(self):
        session = Session()
        for index in range(5):
            session.record(iteration=index)

        events = session.events(limit=2)

        assert [event["iteration"] for event in events] == [3, 4]

    def test_output_is_capped(self):
        session = Session()

        session.record(output="x" * (OUTPUT_LIMIT + 10))

        [event] = session.events()
        assert event["output"].startswith("x" * OUTPUT_LIMIT)
        assert "chars total" in event["output"]

    def test_caps_non_string_output(self):
        session = Session()

        session.record(output=["y"] * OUTPUT_LIMIT)

        [event] = session.events()
        assert "chars total" in event["output"]

    def test_error_is_recorded(self):
        session = Session()

        session.record(error="RuntimeError: boom", data=None)

        [event] = session.events()
        assert event["error"] == "RuntimeError: boom"
        assert event["data"] is None

    def test_artifacts(self):
        session = Session()

        session.artifact("/tmp/a.txt", size=12, note="bash_exec")

        [artifact] = session.artifacts()
        assert artifact["path"] == "/tmp/a.txt"
        assert artifact["bytes"] == 12
        assert artifact["note"] == "bash_exec"

    def test_keys_isolate_sessions(self):
        session = Session()
        other = Session()

        session.record(output="mine")
        other.record(output="theirs")

        assert [event["output"] for event in session.events()] == ["mine"]
        assert [event["output"] for event in other.events()] == ["theirs"]

    def test_clear_removes_rows(self):
        session = Session()
        session.record()
        session.artifact("a")

        session.clear()

        assert session.events() == []
        assert session.artifacts() == []

    def test_persistent_database(self, tmp_path: Path):
        path = str(tmp_path / "runs.db")
        first = Session(path, key="run")
        first.record(output="kept")
        first.close()

        second = Session(path, key="run")

        assert [event["output"] for event in second.events()] == ["kept"]

    def test_close_is_idempotent_and_context_managed(self, tmp_path: Path):
        with Session(str(tmp_path / "x.db")) as session:
            session.record()
            session.close()
            session.close()
        assert session.key
