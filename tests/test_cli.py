"""Tests for dspy_agent_tools.cli."""

import inspect
import re
import sys
from pathlib import Path
from typing import Any, cast

import dspy
import pytest
from plumbum import CommandNotFound

import dspy_agent_tools.cli as cli_module
from dspy_agent_tools import CLI, Arg, Argument, CLITools, Command, Option, Toolbox
from dspy_agent_tools.cli import (
    _default_prefix,
    _description,
    _Flag,
    _flag_was_rejected,
    _json_document,
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
  jsonable  Emit JSON
  legacy    Legacy command without JSON

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

LEGACY = """Legacy command without JSON.

Usage:
  fake-cli legacy
"""

args = sys.argv[1:]
help_flag = next((arg for arg in args if arg in ("--help", "-h")), None)
if help_flag:
    path = [arg for arg in args if arg not in ("--help", "-h", "--json")]
    if "--json" in args and path == ["pr", "list"]:
        sys.stdout.write('{"usage": "fake-cli pr list", "flags": ["--limit"]}')
        raise SystemExit(0)
    if "--json" in args and path == ["legacy"]:
        print("unknown flag: --json")
        raise SystemExit(2)
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
    elif path == ["legacy"]:
        sys.stdout.write(LEGACY)
    else:
        sys.stdout.write("unknown command\n")
        raise SystemExit(1)
    raise SystemExit(0)

json_requested = "--json" in args
if args and args[0] == "legacy":
    if json_requested:
        print("unknown flag: --json")
        print()
        print("Usage: fake-cli <command> <subcommand> [flags]")
        print()
        print("Available commands:")
        print("  legacy")
        raise SystemExit(2)
    print("legacy ok")
elif args and args[0] == "jsonable":
    if json_requested:
        print(f'{{"ok": true, "json_args": {args.count("--json")}}}')
    else:
        print("jsonable ok")
else:
    args = [arg for arg in args if arg != "--json"]
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


def parse_flag(head: str, description: str = "") -> _Flag:
    flag = _parse_flag_head(head, description)
    assert flag is not None
    return flag


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

    def test_parse_help_falls_back_for_root_help(self):
        usage, _, commands, _ = _parse_help(
            "usage: prog <command>\n\n  alpha    Alpha command\n",
            is_root=True,
        )
        assert usage == "prog <command>"
        assert commands == {"alpha": "Alpha command"}


class TestDiscoveryEdges:
    @staticmethod
    def discovery(json_flag=None):
        return cli_module._Discovery(
            sys.executable,
            base_args=[],
            cwd=None,
            env=None,
            timeout=1,
            help_timeout=1,
            max_expands=2,
            max_depth=2,
            json_flag=json_flag,
        )

    def test_help_text_reports_execution_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no help")

        monkeypatch.setattr(cli_module, "_execute", boom)
        text, code, json_help = self.discovery()._help_text(())
        assert text == "no help"
        assert code == 1
        assert json_help == ""

    def test_help_text_reports_json_attempt_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no help")

        monkeypatch.setattr(cli_module, "_execute", boom)
        text, code, json_help = self.discovery(json_flag="--json")._help_text(())
        assert text == "no help"
        assert code == 1
        assert json_help == ""

    def test_json_document_detection(self):
        assert _json_document('{"ok": true}') == '{"ok": true}'
        assert _json_document("{not json") is None
        assert _json_document("Usage: fake-cli") is None

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
            raise CommandNotFound("fake", [])

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


class TestExplicitCommands:
    def test_commands_replace_discovery(self, tmp_path):
        tools = make(
            tmp_path,
            commands=[
                Command(command=("status",), name="status", description="Show status")
            ],
        )
        assert isinstance(tools, CLITools)
        assert [tool.name for tool in tools] == ["fake_status"]
        assert tools._discovery is None
        assert tools[0]().startswith("status ok")
        tools.refresh()

    def test_builds_typed_schema_and_signature(self, tmp_path):
        command = Command(
            command=("pr", "list"),
            name="list_prs",
            description="List pull requests.",
            args={
                "limit": Arg(flag="--limit", type=int, default=30, help="Max rows"),
                "state": Arg(
                    flag="--state", choices=("open", "closed"), default="open"
                ),
                "label": Arg(flag="--label", repeatable=True),
                "json": Arg(flag="--json", type=bool),
            },
            fixed=("--fixed",),
        )
        tool = make(tmp_path, commands=[command])[0]
        assert tool.args == {
            "limit": {"type": "integer", "default": 30},
            "state": {
                "type": "string",
                "enum": ["open", "closed"],
                "default": "open",
            },
            "label": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "json": {"type": "boolean", "default": False},
        }
        assert str(inspect.signature(tool.func)) == (
            "(limit: int = 30, state: str = 'open', "
            "label: list[str] = (), json: bool = False)"
        )

    def test_maps_arguments_to_argv(self, tmp_path):
        command = Command(
            command=("pr", "list"),
            name="list_prs",
            description="List pull requests.",
            args={
                "limit": Arg(flag="--limit", type=int, default=30),
                "state": Arg(
                    flag="--state", choices=("open", "closed"), default="open"
                ),
                "label": Arg(flag="--label", repeatable=True),
                "draft": Arg(flag="--draft", type=bool),
            },
            fixed=("--fixed",),
        )
        tool = make(tmp_path, commands=[command])[0]
        result = tool(limit=2, state="closed", label=["a", "b"], draft=True)
        assert result.startswith(
            "pr list --fixed --limit 2 --state closed --label a --label b --draft"
        )
        assert tool(limit=2).startswith("pr list --fixed --limit 2 --state open")

    def test_positionals_and_variadic_positional(self, tmp_path):
        tools = make(
            tmp_path,
            commands=[
                Command(
                    command=("view",),
                    name="view",
                    description="View a pull request.",
                    args={
                        "number": Arg(positional=0, type=int, help="PR number"),
                        "stat": Arg(flag="--stat", type=bool),
                    },
                ),
                Command(
                    command=("many",),
                    name="many",
                    description="Many paths.",
                    args={"paths": Arg(positional=0, repeatable=True)},
                ),
            ],
        )
        assert tools[0](number=5, stat=True).startswith("ran view 5 --stat")
        assert tools[1](paths=["a", "b"]).startswith("ran many a b")

    def test_choices_and_required_are_validated_in_runner(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command=("pr", "list"),
                    name="list_prs",
                    description="List.",
                    args={
                        "state": Arg(
                            flag="--state", choices=("open", "closed"), default="open"
                        ),
                        "limit": Arg(flag="--limit", type=int),
                    },
                )
            ],
        )[0]
        with pytest.raises(ValueError, match="state must be one of"):
            tool.func(state="bad", limit=1)
        with pytest.raises(ValueError, match="limit is required"):
            tool.func()

    def test_string_path_and_argument_option_helpers(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command="pr list",
                    name="pr_list",
                    description="List pull requests.",
                    args={
                        "state": Option("open", "--state"),
                        "repo": Argument(help="owner/name"),
                        "verbose": Option("--verbose", is_flag=True),
                        "limit": Option("--limit", type=int, default=30),
                    },
                )
            ],
        )[0]
        result = tool(repo="owner/name", state="closed", verbose=True, limit=2)
        assert result.startswith(
            "pr list owner/name --state closed --verbose --limit 2"
        )
        migrate = make(
            tmp_path,
            commands=[
                Command(
                    command="db:migrate", name="migrate", description="Run migrations."
                )
            ],
        )[0]
        assert migrate().startswith("ran db:migrate")

    def test_argument_and_option_required_markers(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command="pr list",
                    name="pr_list",
                    description="List pull requests.",
                    args={
                        "repo": Argument(..., help="owner/name"),
                        "state": Option(..., "--state"),
                    },
                )
            ],
        )[0]
        assert tool.args["repo"] == {
            "type": "string",
        }
        assert tool.args["state"] == {
            "type": "string",
        }
        with pytest.raises(ValueError, match="repo is required"):
            tool.func()
        with pytest.raises(ValueError, match="state is required"):
            tool.func(repo="owner/name")

    def test_option_validates_flag_declarations(self):
        with pytest.raises(ValueError, match="at least one"):
            Option()
        with pytest.raises(ValueError, match="must start with"):
            Option("--state", "closed")
        with pytest.raises(ValueError, match="given twice"):
            Option("open", "--state", default="closed")
        with pytest.raises(ValueError, match="given twice"):
            Option(..., "--state", default="open")

    def test_skipped_positional_is_rejected(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command=("pair",),
                    name="pair",
                    description="Pair.",
                    args={
                        "first": Arg(positional=0, default=None),
                        "second": Arg(positional=1, default=None),
                    },
                )
            ],
        )[0]
        with pytest.raises(ValueError, match="cannot be skipped"):
            tool.func(second="b")

    def test_ok_codes_suppress_exit_notice(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command=("fail",),
                    name="fail_ok",
                    description="Fail with an accepted code.",
                    ok_codes=frozenset({3}),
                )
            ],
        )[0]
        result = tool()
        assert "boom" in result
        assert "[exit code" not in result

    def test_repeatable_defaults_and_none_are_optional(self, tmp_path):
        tool = make(
            tmp_path,
            commands=[
                Command(
                    command=("filtered",),
                    name="filtered",
                    description="Filtered.",
                    args={
                        "label": Arg(
                            flag="--label", repeatable=True, default=["default"]
                        ),
                        "maybe": Arg(flag="--maybe", default=None),
                    },
                )
            ],
        )[0]
        assert tool().startswith("ran filtered --label default")
        assert tool(label=["a"]).startswith("ran filtered --label a")

    @pytest.mark.parametrize(
        ("command", "error", "match"),
        [
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(flag="--x", positional=0)},
                ),
                ValueError,
                "exactly one",
            ),
            (
                Command(command=("bad",), name="bad", args={"x": Arg()}),
                ValueError,
                "exactly one",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(flag="--x", type=bytes)},
                ),
                ValueError,
                "unsupported type",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(flag="--x", type=int, choices=("1", "2"))},
                ),
                ValueError,
                "choices require type=str",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(flag="--x", repeatable=True, default="x")},
                ),
                ValueError,
                "repeatable defaults",
            ),
            (
                Command(command=("bad",), name="bad", args={"x": Arg(flag="x")}),
                ValueError,
                "must start with",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(flag="--x"), "y": Arg(flag="--x")},
                ),
                ValueError,
                "duplicate flag",
            ),
            (
                Command(
                    command=("bad",), name="bad", args={"not-valid": Arg(flag="--x")}
                ),
                ValueError,
                "valid identifier",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(positional=0), "y": Arg(positional=0)},
                ),
                ValueError,
                "duplicate positional",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(positional=0), "y": Arg(positional=2)},
                ),
                ValueError,
                "contiguous",
            ),
            (
                Command(command=("bad",), name="bad", args={"x": Arg(positional=-2)}),
                ValueError,
                "non-negative",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={"x": Arg(positional=0, default=None), "y": Arg(positional=1)},
                ),
                ValueError,
                "cannot follow an optional",
            ),
            (
                Command(
                    command=("bad",),
                    name="bad",
                    args={
                        "x": Arg(positional=0, repeatable=True),
                        "y": Arg(positional=1),
                    },
                ),
                ValueError,
                "must be last",
            ),
            (
                Command(command=("bad",), name="bad name"),
                ValueError,
                "valid identifier",
            ),
            (Command(command=("",), name="bad"), ValueError, "non-empty strings"),
            (
                Command(command=("bad",), name="bad", fixed=cast(Any, (1,))),
                TypeError,
                "must be strings",
            ),
            (
                Command(command=("bad",), name="bad", fixed=cast(Any, "--flag")),
                TypeError,
                "sequence of strings",
            ),
        ],
    )
    def test_rejects_invalid_definitions(self, tmp_path, command, error, match):
        with pytest.raises(error, match=match):
            make(tmp_path, commands=[command])

    def test_rejects_empty_and_duplicate_commands(self, tmp_path):
        with pytest.raises(ValueError, match="at least one"):
            CLI(
                sys.executable,
                base_args=[],
                commands=[],
            )
        with pytest.raises(ValueError, match="Duplicate tool name"):
            make(
                tmp_path,
                commands=[
                    Command(command=("status",), name="same"),
                    Command(command=("where",), name="same"),
                ],
            )

    def test_env_and_cwd_are_forwarded_to_explicit_commands(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        tool = make(
            tmp_path,
            env={"FAKE_ENV": "hello"},
            cwd=workdir,
            commands=[
                Command(
                    command=("where",), name="where", description="Show environment"
                )
            ],
        )[0]
        result = tool()
        assert "env=hello" in result
        assert f"cwd={workdir.resolve()}" in result


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
        tools = by_name(make(tmp_path, json_flag="--json"))
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
