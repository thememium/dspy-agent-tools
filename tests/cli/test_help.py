"""Tests for help parsing and JSON help/flags."""

import pytest

from dspy_agent_tools import Command
from dspy_agent_tools.cli.help import (
    _description,
    _Flag,
    _parse_commands,
    _parse_flag_head,
    _parse_flags,
    _parse_help,
    _usage,
)
from dspy_agent_tools.cli.runner import _argv_hint, _flag_was_rejected

from .helpers import by_name, make, parse_flag


class TestParsingEdges:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "usage: prog [opts]\n  continuation\nFLAGS\n",
                "prog [opts]\ncontinuation",
            ),
            ("usage: prog\n  continuation\nprose\n", "prog\ncontinuation"),
            ("USAGE\n  prog <command>\n  more\n\n", "prog <command>\nmore"),
            ("USAGE\n  prog\nFLAGS\n", "prog"),
            ("no usage here", ""),
        ],
    )
    def test_usage_variants(self, text, expected):
        assert _usage(text) == expected

    def test_description_skips_usage_and_sections(self):
        assert _description("usage: prog\n\nDesc here") == "Desc here"
        assert _description("USAGE\n  prog x\n\nDesc here") == "Desc here"
        assert _description("usage: prog\n  continuation\nDesc here") == "Desc here"
        assert _description("-a    flag\nDesc here") == "Desc here"
        assert _description("Options:\nDesc here") == "Desc here"

    def test_description_prefers_indented_fallback(self):
        assert _description("  Indented description") == "Indented description"
        assert _description("") == ""

    def test_flag_line_includes_repeatable(self):
        flag = _Flag(
            names=["--tag"],
            kind="array",
            default=None,
            choices=None,
            repeatable=True,
            description="",
        )
        assert "repeatable" in flag.line()

    def test_flag_head_skips_non_flags(self):
        assert _parse_flag_head("plain", "") is None

    def test_flag_head_skips_empty_alias_parts(self):
        flag = _parse_flag_head("-x, , --y", "desc")
        assert flag is not None
        assert flag.names == ["-x", "--y"]

    def test_flag_head_parses_optional_values(self):
        flag = _parse_flag_head("--porcelain[=<version>]", "machine readable")
        assert flag is not None
        assert flag.names == ["--porcelain"]
        assert flag.kind == "string"

    def test_flag_head_parses_inline_defaults(self):
        assert parse_flag("--mode=auto").default == "auto"
        assert parse_flag("--flag=true").kind == "boolean"
        assert parse_flag("--flag=[]").kind == "array"
        assert parse_flag("--flag=map[]").kind == "object"
        assert parse_flag("--flag=").kind == "string"
        assert parse_flag("--flag=500").kind == "integer"

    def test_flag_head_parses_repeatable_names(self):
        flag = _parse_flag_head("--tag...", "")
        assert flag is not None
        assert flag.repeatable is True

    def test_flag_head_parses_repeatable_values(self):
        flag = _parse_flag_head("--flag <values>...", "")
        assert flag is not None
        assert flag.repeatable is True
        assert flag.kind == "array"

    def test_flag_head_treats_usage_template_as_value(self):
        flag = _parse_flag_head("--engine=ENGINE", "")
        assert flag is not None
        assert flag.default is None
        assert flag.kind == "string"

    def test_flag_head_infers_object_and_array(self):
        assert parse_flag("--map map").kind == "object"
        array = parse_flag("--list list")
        assert array.kind == "array"
        assert array.repeatable is True

    def test_parse_flags_joins_continuations_and_ignores_duplicates(self):
        flags = {}
        _parse_flags(
            ["  --x int   first", "             more", "  --x int   second"],
            flags,
        )
        assert flags["--x"].description == "first more"

    def test_parse_commands_skips_unmatched_and_meta(self):
        commands = {}
        _parse_commands(["  nope"], commands)
        _parse_commands(["  help    Show help"], commands)
        _parse_commands(["  real    Real command"], commands)
        assert commands == {"real": "Real command"}

    def test_parse_help_reads_arguments_section(self):
        _usage, _desc, _commands, _flags, positionals = _parse_help(
            "Usage: prog search [OPTIONS] [QUERY]\n"
            "\nArguments:\n  query    Text to find\n"
            "           across sessions\njunk\n\n",
            is_root=False,
        )
        assert positionals == {"query": "Text to find across sessions"}

    def test_argv_hint_only_after_failure(self):
        assert _argv_hint("usage: ok", ["cmd"], 0) == "usage: ok"
        hinted = _argv_hint("usage: bad", ["cmd", "two words"], 2)
        assert "[argv: cmd 'two words']" in hinted
        assert "one item" in hinted

    def test_value_flags_are_not_boolean(self):
        assert parse_flag("--limit").kind == "value"
        assert parse_flag("--count").kind == "value"
        assert parse_flag("--verbose").kind == "boolean"

    def test_parse_help_falls_back_for_root_help(self):
        usage, _, commands, _, _ = _parse_help(
            "usage: prog <command>\n\n  alpha    Alpha command\n",
            is_root=True,
        )
        assert usage == "prog <command>"
        assert commands == {"alpha": "Alpha command"}


class TestJsonFlag:
    def test_tries_json_when_supported(self, tmp_path):
        tool = make(
            tmp_path,
            json_flag="--json",
            commands=[Command(command=("jsonable",), name="jsonable")],
        )[0]
        result = tool()
        assert result.startswith('{"ok": true, "json_args": 1}')

    def test_falls_back_when_json_is_rejected(self, tmp_path):
        tool = make(
            tmp_path,
            json_flag="--json",
            commands=[Command(command=("legacy",), name="legacy")],
        )[0]
        assert tool().startswith("legacy ok")

    def test_discovery_run_tries_json_and_falls_back(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json"))
        json_result = tools["fake_run_command"](command="jsonable", args=[])
        assert json_result.startswith('{"ok": true, "json_args": 1}')
        legacy_result = tools["fake_run_command"](command="legacy", args=[])
        assert legacy_result.startswith("legacy ok")

    def test_json_is_off_by_default(self, tmp_path):
        tools = by_name(make(tmp_path))
        assert "JSON help:" not in tools["fake_get_command_help"](command="pr list")
        assert tools["fake_run_command"](command="jsonable", args=[]).startswith(
            "jsonable ok"
        )
        tool = make(
            tmp_path,
            commands=[Command(command=("jsonable",), name="jsonable")],
        )[0]
        assert tool().startswith("jsonable ok")

    def test_explicit_json_is_not_duplicated(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_run_command"](command="jsonable", args=["--json"])
        assert result.startswith('{"ok": true, "json_args": 1}')

    def test_real_failure_is_not_retried(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json"))
        result = tools["fake_run_command"](command="fail", args=[])
        assert "[exit code 3]" in result

    def test_rejects_invalid_json_flag(self, tmp_path):
        with pytest.raises(ValueError, match="json_flag"):
            make(tmp_path, json_flag="json")

    def test_recognizes_flag_without_dashes(self):
        assert _flag_was_rejected("error: unknown option `json'", "--json")
        assert _flag_was_rejected(
            "error: Found argument '--json' which wasn't expected", "--json"
        )
        assert not _flag_was_rejected("error: unknown option `other'", "--json")
        assert not _flag_was_rejected("real command failure", "--json")


class TestJsonHelp:
    def test_details_include_json_help_when_available(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json"))
        result = tools["fake_get_command_help"](command="pr list")
        assert "JSON help:" in result
        assert '"usage": "fake-cli pr list"' in result
        assert "Flags:" in result

    def test_search_still_uses_text_help(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json", include_search=True))
        result = tools["fake_search_commands"](query="pull requests")
        assert result.splitlines()[0].startswith("1. pr list")

    def test_code_mode_includes_json_help(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json", mode="code"))
        result = tools["fake_get_command_help"](command="pr list")
        assert '"usage": "fake-cli pr list"' in result["json_help"]

    def test_json_help_is_truncated(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json", max_help_chars=10))
        result = tools["fake_get_command_help"](command="pr list")
        assert "JSON help:" in result
        assert "[help truncated]" in result

        code = by_name(
            make(
                tmp_path,
                json_flag="--json",
                mode="code",
                max_help_chars=10,
            )
        )
        record = code["fake_get_command_help"](command="pr list")
        assert record["json_help"].endswith("[help truncated]")

    def test_json_help_falls_back_when_rejected(self, tmp_path):
        tools = by_name(make(tmp_path, json_flag="--json"))
        result = tools["fake_get_command_help"](command="legacy")
        assert "Legacy command without JSON." in result
        assert "JSON help:" not in result
