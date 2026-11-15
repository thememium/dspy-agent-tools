"""Shared helpers for CLI tests."""

import sys
from pathlib import Path

import dspy

from dspy_agent_tools import CLI, CLITools
from dspy_agent_tools.cli.help import _Flag, _parse_flag_head

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
  no_help   Command without a help flag

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

Arguments:
  query                Search query
"""

LEGACY = """Legacy command without JSON.

Usage:
  fake-cli legacy
"""

args = sys.argv[1:]
help_flag = next((arg for arg in args if arg in ("--help", "-h", "help")), None)
if help_flag:
    path = [arg for arg in args if arg not in ("--help", "-h", "help", "--json")]
    if "--json" in args and path == ["pr", "list"]:
        sys.stdout.write('{"usage": "fake-cli pr list", "flags": ["--limit"]}')
        raise SystemExit(0)
    if "--json" in args and path == ["legacy"]:
        print("unknown flag: --json")
        raise SystemExit(2)
    if path == ["no_help"]:
        print(f"unknown flag: {help_flag}")
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
if args == ["no_help"]:
    print("no_help output")
elif args and args[0] == "legacy":
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
