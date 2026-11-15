"""Tests for lazy command discovery and caching."""

import sys

import pytest
from plumbum import CommandNotFound

import dspy_agent_tools.cli.discovery as discovery_module
import dspy_agent_tools.cli.runner as runner_module
from dspy_agent_tools.cli.argv import _normalize_args
from dspy_agent_tools.cli.runner import _json_document

from .helpers import by_name, make


class TestSearch:
    def test_discovers_nested_commands(self, tmp_path):
        tools = by_name(make(tmp_path, include_search=True))
        result = tools["fake_search_commands"](query="pull requests")
        assert result.splitlines()[0] == "1. pr list: List pull requests"
        assert "pr view" in result

    def test_reports_no_match(self, tmp_path):
        tools = by_name(make(tmp_path, include_search=True))
        result = tools["fake_search_commands"](query="zzzz")
        assert result.startswith("[no matching commands;")
        assert "get_command_help" in result

    def test_scores_exact_path_and_name(self, tmp_path):
        tools = by_name(make(tmp_path, include_search=True))
        assert tools["fake_search_commands"](query="status").startswith("1. status")
        assert tools["fake_search_commands"](query="pr list").startswith("1. pr list")

    def test_empty_query_lists_root_commands(self, tmp_path):
        tools = by_name(make(tmp_path, include_search=True))
        result = tools["fake_search_commands"](query="")
        assert "status" in result
        assert "pr" in result

    def test_root_only_cli_returns_root(self, tmp_path):
        tools = by_name(
            make(
                tmp_path,
                env={"FAKE_ROOT_ONLY": "1"},
                include_search=True,
            )
        )
        result = tools["fake_search_commands"](query="anything")
        assert result == "1. (root): root-only tool."


class TestDetails:
    def test_unknown_command_raises(self, tmp_path):
        tools = by_name(make(tmp_path))
        with pytest.raises(ValueError, match="Unknown command 'nope'"):
            tools["fake_get_command_help"](command="nope")


class TestCache:
    def test_refresh_clears_expanded_help(self, tmp_path):
        tools = make(tmp_path)
        tools[0](command="")
        discovery = tools._discovery
        assert discovery is not None
        assert discovery.root.expanded

        tools.refresh()
        assert not discovery.root.expanded


class TestDiscoveryEdges:
    @staticmethod
    def discovery(json_flag=None, help_tokens=("--help",)):
        return discovery_module._Discovery(
            sys.executable,
            base_args=[],
            cwd=None,
            env=None,
            timeout=1,
            help_timeout=1,
            max_expands=2,
            max_depth=2,
            help_tokens=help_tokens,
            json_flag=json_flag,
        )

    def test_help_text_reports_execution_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no help")

        monkeypatch.setattr(runner_module, "execute", boom)
        text, code, json_help = self.discovery()._help_text(())
        assert text == "no help"
        assert code == 1
        assert json_help == ""

    def test_help_text_reports_json_attempt_errors(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no help")

        monkeypatch.setattr(runner_module, "execute", boom)
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
        node = discovery_module._Node("status", ("status",))
        assert discovery_module._Discovery._score(node, ["sta"], "sta") > 0
        assert discovery_module._Discovery._score(node, ["atu"], "atu") > 0

    def test_search_honors_expansion_budget(self, tmp_path):
        tools = by_name(make(tmp_path, max_expands=1, include_search=True))
        assert tools["fake_search_commands"](query="pull requests")

    def test_run_reports_command_not_found(self, monkeypatch, tmp_path):
        tools = by_name(make(tmp_path))
        tools["fake_get_command_help"](command="")

        def boom(*args, **kwargs):
            raise CommandNotFound("fake", [])

        monkeypatch.setattr(runner_module, "execute", boom)
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
