"""Tests for running discovered command paths."""

import re
from typing import Any, cast

import pytest

import dspy_agent_tools.cli.discovery as discovery_module
from dspy_agent_tools import Command

from .helpers import by_name, make


class TestRun:
    def test_runs_resolved_command_with_argv(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_run_command"](command="pr list", args=["--limit", "2"])
        assert result.startswith("pr list --limit 2")
        assert re.search(r"\(\d+\.\d+s\)$", result.rstrip())

    def test_nonzero_exit_is_reported(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_run_command"](command="fail", args=[])
        assert "boom" in result
        assert "[exit code 3]" in result

    def test_usage_error_includes_command_help(self, monkeypatch, tmp_path):
        tools = by_name(make(tmp_path))
        monkeypatch.setattr(
            discovery_module,
            "_run_argv",
            lambda *args, **kwargs: "usage: nope\n[exit code 2]\n(0.1s)",
        )
        result = tools["fake_run_command"](command="pr list", args=[])
        assert "[command help]" in result
        assert "--limit" in result

    def test_output_can_be_capped(self, tmp_path):
        tools = by_name(make(tmp_path, max_output_chars=5))
        result = tools["fake_run_command"](command="status", args=[])
        assert "[output truncated:" in result
        assert "status ok" not in result

    def test_explicit_output_can_be_capped(self, tmp_path):
        tools = make(
            tmp_path,
            max_output_chars=5,
            commands=[Command(command=("status",), name="status", description="Show")],
        )
        result = tools[0]()
        assert "[output truncated:" in result
        assert "status ok" not in result

    def test_empty_command_runs_root(self, tmp_path):
        tools = by_name(make(tmp_path, env={"FAKE_ROOT_ONLY": "1"}))
        result = tools["fake_run_command"](command="", args=["abc"])
        assert result.startswith("ran abc")

    def test_env_and_cwd_are_forwarded(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        tools = by_name(make(tmp_path, env={"FAKE_ENV": "hello"}, cwd=workdir))
        result = tools["fake_run_command"](command="where", args=[])
        assert "env=hello" in result
        assert f"cwd={workdir.resolve()}" in result

    def test_custom_short_help_flag(self, tmp_path):
        tools = by_name(
            make(
                tmp_path,
                env={"FAKE_MAN": "1"},
                help_flag=("-h",),
            )
        )
        result = tools["fake_get_command_help"](command="pr")
        assert "Usage:" in result
        assert "Manage pull requests" in result

    def test_help_argument(self, tmp_path):
        tools = by_name(make(tmp_path, help_flag="help"))
        result = tools["fake_get_command_help"](command="")
        assert "fake-cli does fake things." in result

    def test_falls_back_to_main_command_when_help_rejected(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_get_command_help"](command="no_help")
        assert "no_help output" in result

    def test_help_flag_none_runs_command(self, tmp_path):
        tools = make(tmp_path, help_flag=None)
        discovery = tools._discovery
        assert discovery is not None
        text, _code, _json_help = discovery._help_text(("no_help",))
        assert "no_help output" in text

    def test_man_page_is_returned_when_no_short_help_configured(self, tmp_path):
        tools = make(tmp_path, env={"FAKE_MAN": "1"})
        discovery = tools._discovery
        assert discovery is not None
        text, _code, _json_help = discovery._help_text(("pr",))
        assert "fake-cli" in text

    def test_rejects_invalid_help_flag(self, tmp_path):
        with pytest.raises(ValueError, match="help_flag"):
            make(tmp_path, help_flag="")
        with pytest.raises(ValueError, match="help_flag"):
            make(tmp_path, help_flag=cast(Any, (1,)))
