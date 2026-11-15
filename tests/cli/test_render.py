"""Tests for text rendering of discovered command trees."""

from .helpers import by_name, make


class TestDetails:
    def test_includes_usage_children_and_flags(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_get_command_help"](command="pr list")
        assert "Usage:" in result
        assert "--limit (integer; default: 30)" in result
        assert '--state (string; choices: open|closed; default: "open")' in result
        assert "Arguments:" in result
        assert "query — Search query" in result

    def test_lists_subcommands_with_descriptions(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_get_command_help"](command="pr")
        assert "Subcommands:" in result
        assert "list — List pull requests" in result

    def test_root_help_uses_empty_command(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_get_command_help"](command="")
        assert result.startswith("(root): fake-cli does fake things.")
        assert "--verbose (boolean)" in result

    def test_raw_help_can_be_truncated(self, tmp_path):
        tools = by_name(make(tmp_path, max_help_chars=10))
        result = tools["fake_get_command_help"](command="no_help")
        assert "Raw help:" in result
        assert result.endswith("[help truncated]")
