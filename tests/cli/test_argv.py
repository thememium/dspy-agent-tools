"""Tests for typed explicit command argv and validation."""

import inspect
import sys
from typing import Any, cast

import pytest

from dspy_agent_tools import CLI, Argument, CLITools, Command, Option
from dspy_agent_tools.cli.types import Arg

from .helpers import make


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
