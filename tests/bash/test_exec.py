"""Tests for dspy_agent_tools.bash.exec."""

import os
import subprocess
import sys

import pytest

from dspy_agent_tools.bash import bash_exec


class TestBashExec:
    def test_simple_command(self):
        result = bash_exec("echo hello")
        assert result.startswith("hello\n(")
        assert result.endswith("s)")

    def test_cwd_argument(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here")
        result = bash_exec("cat marker.txt", cwd=str(tmp_path))
        assert result.startswith("here")

    def test_leading_cd_is_used_as_cwd(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here")
        result = bash_exec(f"cd {tmp_path} && cat marker.txt")
        assert result.startswith("here")

    def test_quoted_cd_stays_with_the_shell(self, tmp_path):
        # A quoted path is not captured by _CD_PATTERN; the shell handles it.
        d = tmp_path / "with space"
        d.mkdir()
        (d / "marker.txt").write_text("here")
        result = bash_exec(f'cd "{d}" && cat marker.txt')
        assert result.startswith("here")

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout must be >= 0"):
            bash_exec("echo hi", timeout=-1)

    def test_zero_timeout_disables_deadline(self):
        result = bash_exec("echo zero", timeout=0)
        assert result.startswith("zero")

    def test_timeout_kills_and_reports(self):
        result = bash_exec("echo partial && sleep 30", timeout=1)
        assert result.startswith("partial")
        assert "command timed out after 1s and was killed" in result

    def test_nonzero_exit_is_a_notice(self):
        result = bash_exec("echo out >&2; exit 3")
        assert "[exit code 3]" in result
        assert "out" in result

    def test_large_output_is_returned_in_full(self):
        result = bash_exec("python3 -c \"print('x' * 40000)\"")
        assert "x" * 40000 in result
        assert "truncated" not in result

    def test_wall_time_suffix(self):
        result = bash_exec("true")
        assert result.endswith("s)") and "(" in result


def test_package_import_survives_broken_rgapi(tmp_path):
    """An unloadable rgapi extension must not break the whole package."""
    (tmp_path / "rgapi.py").write_text(
        "raise ImportError('rgapi native extension failed to load')\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import dspy_agent_tools; print(dspy_agent_tools.web_search.__name__)",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "web_search"
