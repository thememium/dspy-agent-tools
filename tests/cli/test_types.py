"""Tests for dspy_agent_tools.cli.types and the CLI factory."""

import dspy
import pytest

from dspy_agent_tools import CLI, CLITools
from dspy_agent_tools.cli.tools import _default_prefix

from .helpers import make


class TestFactory:
    def test_returns_help_and_run_by_default(self, tmp_path):
        tools = make(tmp_path)
        assert isinstance(tools, CLITools)
        assert isinstance(tools, list)
        assert [tool.name for tool in tools] == [
            "fake_get_command_help",
            "fake_run_command",
        ]
        assert all(isinstance(tool, dspy.Tool) for tool in tools)

    def test_include_search_adds_search_tool(self, tmp_path):
        tools = make(tmp_path, include_search=True)
        assert {tool.name for tool in tools} == {
            "fake_search_commands",
            "fake_get_command_help",
            "fake_run_command",
        }

    def test_missing_executable_raises(self):
        with pytest.raises(FileNotFoundError, match="Executable not found"):
            CLI("definitely-not-a-real-binary-xyz", name_prefix="x_")

    def test_default_prefix_is_sanitized(self):
        assert _default_prefix("/usr/local/bin/gh") == "gh_"
        assert _default_prefix("my-tool") == "my_tool_"

    def test_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown mode"):
            make(tmp_path, mode="eager")

    def test_rejects_negative_output_cap(self, tmp_path):
        with pytest.raises(ValueError, match="max_output_chars"):
            make(tmp_path, max_output_chars=-1)
