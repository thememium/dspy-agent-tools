"""Tests for the discovery tool facade and code-mode tools."""

from dspy_agent_tools import Toolbox

from .helpers import by_name, make


class TestToolboxIntegration:
    def test_toolbox_accepts_the_facade(self, tmp_path):
        box = Toolbox(make(tmp_path, include_search=True))
        assert {tool.name for tool in box} == {
            "fake_search_commands",
            "fake_get_command_help",
            "fake_run_command",
        }


class TestCodeMode:
    def test_search_returns_records(self, tmp_path):
        tools = by_name(make(tmp_path, mode="code", include_search=True))
        result = tools["fake_search_commands"](query="pull requests")
        assert isinstance(result, list)
        assert result[0] == {
            "command": "pr list",
            "description": "List pull requests",
        }

    def test_details_returns_flags_and_children(self, tmp_path):
        tools = by_name(make(tmp_path, mode="code"))
        result = tools["fake_get_command_help"](command="pr list")
        assert isinstance(result, dict)
        assert result["flags"]["--limit"]["type"] == "integer"
        assert result["flags"]["--state"]["choices"] == ["open", "closed"]
        assert result["subcommands"] == {}

        parent = tools["fake_get_command_help"](command="pr")
        assert parent["subcommands"] == {
            "list": "List pull requests",
            "view": "View a pull request",
        }
