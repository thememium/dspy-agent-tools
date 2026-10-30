"""Tests for dspy_agent_tools.cli."""

import re
import sys
from pathlib import Path

import dspy
import pytest
from plumbum import CommandNotFound

import dspy_agent_tools.cli as cli_module
from dspy_agent_tools import CLI, CLITools, Toolbox
from dspy_agent_tools.cli import (
    _default_prefix,
    _description,
    _Flag,
    _normalize_args,
    _parse_commands,
    _parse_flag_head,
    _parse_flags,
    _parse_help,
    _usage,
)

SCRIPT = r'''
import os
import sys

ROOT = """fake-cli does fake things.

Usage:
  fake-cli <command> [flags]

Commands:
  status    Show status
  pr        Manage pull requests
  fail      Fail on purpose
  where     Show environment

Options:
  --verbose    Be loud
"""

ROOT_ONLY = """root-only tool.

Usage:
  fake-cli [OPTIONS] PATTERN

Options:
  -i, --ignore-case    Case insensitive
"""

PR = """Manage pull requests.

Usage:
  fake-cli pr <command> [flags]

Commands:
  list    List pull requests
  view    View a pull request
"""

LIST = """List pull requests.

Usage:
  fake-cli pr list [flags]

Options:
  --limit int          Maximum rows (default 30)
  --state string       Filter state: {open|closed} (default "open")
"""

args = sys.argv[1:]
help_flag = args[-1] if args and args[-1] in ("--help", "-h") else None
if help_flag:
    path = args[:-1]
    if os.environ.get("FAKE_MAN") and help_flag == "--help":
        sys.stdout.write("N\bNA\bAM\bME\n     fake-cli\n")
        raise SystemExit(0)
    if not path:
        sys.stdout.write(
            ROOT_ONLY if os.environ.get("FAKE_ROOT_ONLY") else ROOT
        )
    elif path == ["pr"]:
        sys.stdout.write(PR)
    elif path == ["pr", "list"]:
        sys.stdout.write(LIST)
    else:
        sys.stdout.write("unknown command\n")
        raise SystemExit(1)
    raise SystemExit(0)

if args == ["where"]:
    print("cwd=" + os.getcwd())
    print("env=" + os.environ.get("FAKE_ENV", ""))
elif args == ["fail"]:
    print("boom")
    raise SystemExit(3)
elif args[:2] == ["pr", "list"]:
    print("pr list " + " ".join(args[2:]))
elif args == ["status"]:
    print("status ok")
else:
    print("ran " + " ".join(args))
'''


def make(tmp_path: Path, **kwargs) -> CLITools:
    script = tmp_path / "fake_cli.py"
    script.write_text(SCRIPT)
    return CLI(
        sys.executable,
        base_args=[str(script)],
        name_prefix="fake_",
        **kwargs,
    )


def by_name(tools: CLITools) -> dict[str, dspy.Tool]:
    return {tool.name: tool for tool in tools}


class TestFactory:
    def test_returns_three_dspy_tools(self, tmp_path):
        tools = make(tmp_path)
        assert isinstance(tools, CLITools)
        assert isinstance(tools, list)
        assert {tool.name for tool in tools} == {
            "fake_search_commands",
            "fake_get_command_help",
            "fake_run_command",
        }
        assert all(isinstance(tool, dspy.Tool) for tool in tools)

    def test_missing_executable_raises(self):
        with pytest.raises(FileNotFoundError, match="Executable not found"):
            CLI("definitely-not-a-real-binary-xyz", name_prefix="x_")

    def test_default_prefix_is_sanitized(self):
        assert _default_prefix("/usr/local/bin/gh") == "gh_"
        assert _default_prefix("my-tool") == "my_tool_"

    def test_rejects_unknown_mode(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown mode"):
            make(tmp_path, mode="eager")


class TestToolboxIntegration:
    def test_toolbox_accepts_the_facade(self, tmp_path):
        box = Toolbox(make(tmp_path))
        assert {tool.name for tool in box} == {
            "fake_search_commands",
            "fake_get_command_help",
            "fake_run_command",
        }


class TestSearch:
    def test_discovers_nested_commands(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_search_commands"](query="pull requests")
        assert result.splitlines()[0] == "1. pr list: List pull requests"
        assert "pr view" in result

    def test_reports_no_match(self, tmp_path):
        tools = by_name(make(tmp_path))
        assert tools["fake_search_commands"](query="zzzz") == "[no matching commands]"

    def test_empty_query_lists_root_commands(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_search_commands"](query="")
        assert "status" in result
        assert "pr" in result

    def test_root_only_cli_returns_root(self, tmp_path):
        tools = by_name(make(tmp_path, env={"FAKE_ROOT_ONLY": "1"}))
        result = tools["fake_search_commands"](query="anything")
        assert result == "1. (root): root-only tool."


class TestDetails:
    def test_includes_usage_children_and_flags(self, tmp_path):
        tools = by_name(make(tmp_path))
        result = tools["fake_get_command_help"](command="pr list")
        assert "Usage:" in result
        assert "--limit (integer; default: 30)" in result
        assert '--state (string; choices: open|closed; default: "open")' in result

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
        tools = by_name(make(tmp_path, max_help_chars=40))
        result = tools["fake_get_command_help"](command="pr list")
        assert result.endswith("[help truncated]")

    def test_unknown_command_raises(self, tmp_path):
        tools = by_name(make(tmp_path))
        with pytest.raises(ValueError, match="Unknown command 'nope'"):
            tools["fake_get_command_help"](command="nope")


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

    def test_man_page_help_falls_back_to_short_help(self, tmp_path):
        tools = by_name(make(tmp_path, env={"FAKE_MAN": "1"}))
        result = tools["fake_get_command_help"](command="pr")
        assert "Usage:" in result
        assert "Manage pull requests." in result


class TestCodeMode:
    def test_search_returns_records(self, tmp_path):
        tools = by_name(make(tmp_path, mode="code"))
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


class TestCache:
    def test_refresh_clears_expanded_help(self, tmp_path):
        tools = make(tmp_path)
        tools[0](query="status")
        discovery = tools._discovery
        assert discovery is not None
        assert discovery.root.expanded

        tools.refresh()
        assert not discovery.root.expanded


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
        assert _parse_flag_head("--mode=auto", "").default == "auto"
        assert _parse_flag_head("--flag=true", "").kind == "boolean"
        assert _parse_flag_head("--flag=[]", "").kind == "array"
        assert _parse_flag_head("--flag=map[]", "").kind == "object"
        assert _parse_flag_head("--flag=", "").kind == "string"
        assert _parse_flag_head("--flag=500", "").kind == "integer"

    def test_flag_head_parses_repeatable_names(self):
        flag = _parse_flag_head("--tag...", "")
        assert flag is not None
        assert flag.repeatable is True

    def test_flag_head_parses_repeatable_values(self):
        flag = _parse_flag_head("--flag <values>...", "")
        assert flag is not None
        assert flag.repeatable is True
        assert flag.kind == "string"

    def test_flag_head_treats_usage_template_as_value(self):
        flag = _parse_flag_head("--engine=ENGINE", "")
        assert flag is not None
        assert flag.default is None
        assert flag.kind == "string"

    def test_flag_head_infers_object_and_array(self):
        assert _parse_flag_head("--map map", "").kind == "object"
        array = _parse_flag_head("--list list", "")
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

    def test_parse_help_falls_back_for_root_help(self):
        usage, _, commands, _ = _parse_help(
            "usage: prog <command>\n\n  alpha    Alpha command\n",
            is_root=True,
        )
        assert usage == "prog <command>"
        assert commands == {"alpha": "Alpha command"}


class TestDiscoveryEdges:
    @staticmethod
    def discovery():
        return cli_module._Discovery(
            sys.executable,
            base_args=[],
            cwd=None,
            env=None,
            timeout=1,
            help_timeout=1,
            max_expands=2,
            max_depth=2,
        )

    def test_help_text_reports_execution_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no help")

        monkeypatch.setattr(cli_module, "_execute", boom)
        text, code = self.discovery()._help_text(())
        assert text == "no help"
        assert code == 1

    def test_expand_unknown_path_raises(self):
        with pytest.raises(KeyError):
            self.discovery().expand(("nope",))

    def test_score_covers_name_affixes(self):
        node = cli_module._Node("status", ("status",))
        assert cli_module._Discovery._score(node, ["sta"], "sta") > 0
        assert cli_module._Discovery._score(node, ["atu"], "atu") > 0

    def test_search_honors_expansion_budget(self, tmp_path):
        tools = by_name(make(tmp_path, max_expands=1))
        assert tools["fake_search_commands"](query="pull requests")

    def test_run_reports_command_not_found(self, monkeypatch, tmp_path):
        tools = by_name(make(tmp_path))
        tools["fake_search_commands"](query="")

        def boom(*args, **kwargs):
            raise CommandNotFound("fake")

        monkeypatch.setattr(cli_module, "_execute", boom)
        result = tools["fake_run_command"](command="", args=[])
        assert result.startswith("[command not found:")

    def test_code_mode_truncates_help(self, tmp_path):
        tools = by_name(make(tmp_path, mode="code", max_help_chars=40))
        result = tools["fake_get_command_help"](command="pr list")
        assert result["help"].endswith("[help truncated]")

    def test_normalize_args_accepts_none_and_string(self):
        assert _normalize_args(None) == []
        assert _normalize_args("--limit 2") == ["--limit", "2"]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"timeout": -1}, "timeout must be"),
            ({"help_timeout": 0}, "help_timeout must be"),
            ({"max_expands": 0}, "max_expands must be"),
            ({"max_depth": 0}, "max_depth must be"),
            ({"max_help_chars": -1}, "max_help_chars must be"),
        ],
    )
    def test_rejects_invalid_options(self, tmp_path, kwargs, message):
        with pytest.raises(ValueError, match=message):
            make(tmp_path, **kwargs)
