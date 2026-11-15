"""Tests for dspy_agent_tools.utils.process."""

import sys

import pytest

from dspy_agent_tools.utils.process import (
    execute,
    render,
)
from dspy_agent_tools.utils.process import (
    resolve_cwd as _resolve_cwd,
)
from dspy_agent_tools.utils.process import (
    resolve_existing as _resolve_existing,
)


class TestResolveExisting:
    def test_none_resolves_to_current_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert _resolve_existing(None) == tmp_path.resolve()

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            _resolve_existing("/nonexistent/definitely/missing")

    def test_existing_file_is_accepted(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert _resolve_existing(str(f)) == f.resolve()

    def test_tilde_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _resolve_existing("~") == tmp_path.resolve()


class TestResolveCwd:
    def test_missing_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            _resolve_cwd("/nonexistent/definitely/missing")

    def test_file_raises_not_a_directory(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            _resolve_cwd(str(f))

    def test_directory_passes(self, tmp_path):
        assert _resolve_cwd(str(tmp_path)) == tmp_path.resolve()


class TestExecute:
    def test_returns_output_and_exit_code(self):
        output, notices, elapsed, code = execute(["echo", "hi"])

        assert output == "hi\n"
        assert notices == []
        assert elapsed >= 0
        assert code == 0

    def test_nonzero_exit_is_a_notice(self):
        output, notices, _elapsed, code = execute(["sh", "-c", "echo out; exit 3"])

        assert output == "out\n"
        assert notices == ["[exit code 3]"]
        assert code == 3

    def test_ok_codes_suppress_the_notice(self):
        _output, notices, _elapsed, code = execute(
            ["sh", "-c", "exit 1"], ok_codes=frozenset({1})
        )

        assert notices == []
        assert code == 1

    def test_cwd_and_env_are_applied(self, tmp_path):
        output, _notices, _elapsed, _code = execute(
            ["sh", "-c", "pwd; echo $MARKER"],
            cwd=tmp_path,
            env={"MARKER": "set"},
        )

        assert output.splitlines() == [str(tmp_path), "set"]

    def test_timeout_kills_the_process_and_reports(self):
        output, notices, _elapsed, _code = execute(
            [
                sys.executable,
                "-c",
                "import time; print('partial', flush=True); time.sleep(30)",
            ],
            timeout=1,
        )

        assert output == "partial\n"
        assert notices == ["[command timed out after 1s and was killed]"]


class TestRender:
    def test_appends_wall_time(self):
        assert render("hi\n", [], 0.1) == "hi\n(0.1s)"

    def test_appends_notices_before_wall_time(self):
        assert render("hi\n", ["[exit code 3]"], 0.2) == ("hi\n\n[exit code 3]\n(0.2s)")
