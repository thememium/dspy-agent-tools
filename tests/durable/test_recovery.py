"""Crash recovery and queued-worker tests run in real subprocesses."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run(
    script: str, mode: str, directory: Path, app: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "DURABLE_DIR": str(directory),
        "DURABLE_APP": app,
    }
    return subprocess.run(
        [sys.executable, str(Path(__file__).with_name(script)), mode],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text().splitlines()


def test_crash_recovery_replays_completed_steps(tmp_path: Path):
    app = f"rec-{uuid.uuid4().hex[:10]}"
    first = _run("recovery_app.py", "first", tmp_path, app)
    assert first.returncode == 17, first.stderr
    assert len(_lines(tmp_path / "lm_calls.txt")) == 2
    assert _lines(tmp_path / "tool_calls.txt") == ["weather:Paris", "crash"]

    recovered = _run("recovery_app.py", "recover", tmp_path, app)
    assert recovered.returncode == 0, recovered.stderr
    assert "Sunny in Paris" in recovered.stdout

    # Two recovered predictions (finish + extract); neither ReAct turn nor the
    # completed weather call was executed again.
    assert len(_lines(tmp_path / "lm_calls.txt")) == 4
    tool_calls = _lines(tmp_path / "tool_calls.txt")
    assert tool_calls == ["weather:Paris", "crash", "crash"]


def test_rlm_crash_recovery_reconstructs_interpreter_state(tmp_path: Path):
    if shutil.which("deno") is None:
        pytest.skip("Deno is required to run the real dspy.RLM crash test")

    app = f"rlm-{uuid.uuid4().hex[:10]}"
    first = _run("rlm_recovery_app.py", "first", tmp_path, app)
    assert first.returncode == 17, first.stderr
    assert len(_lines(tmp_path / "lm_calls.txt")) == 2
    assert _lines(tmp_path / "seed_calls.txt") == ["seed"]
    assert _lines(tmp_path / "crash_calls.txt") == ["crash:seed-value"]

    recovered = _run("rlm_recovery_app.py", "recover", tmp_path, app)
    assert recovered.returncode == 0, recovered.stderr
    assert "seed-value" in recovered.stdout

    # Both action predictions before the crash were checkpointed. Only the
    # third action (SUBMIT) ran in the recovery process.
    assert len(_lines(tmp_path / "lm_calls.txt")) == 3
    # The completed seed tool was replayed, not called again.
    assert _lines(tmp_path / "seed_calls.txt") == ["seed"]
    # The crash tool ran before its checkpoint both times, so it is the one
    # expected duplicate.
    assert _lines(tmp_path / "crash_calls.txt") == [
        "crash:seed-value",
        "crash:seed-value",
    ]


def test_queued_worker_executes(tmp_path: Path):
    app = f"queue-{uuid.uuid4().hex[:10]}"
    result = _run("queue_app.py", "run", tmp_path, app)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "42"
