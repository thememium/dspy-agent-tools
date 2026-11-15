"""Tests for dspy_agent_tools.bash.output."""

from pathlib import Path

import pytest

from dspy_agent_tools.bash.output import (
    _found_paths,
    _glob_matches,
    _has_hidden_component,
    _sum_counts,
)


class TestSearchHelpers:
    def test_sum_counts_ignores_noise(self):
        assert _sum_counts(["a.py:2", "b.py:3", "grep: permission denied"]) == 5

    def test_has_hidden_component(self):
        assert _has_hidden_component(".hidden/a.py:1:x")
        assert _has_hidden_component("sub/.x/b.py:1:x")
        assert not _has_hidden_component("a.py:1:x")
        assert not _has_hidden_component("sub/a.py-2:context")

    def test_glob_matches(self):
        assert _glob_matches(Path("sub/a.py"), "*.py")  # no "/": any depth
        assert _glob_matches(Path("sub/a.py"), "sub/*.py")  # anchored
        assert not _glob_matches(Path("x/sub/a.py"), "sub/*.py")


class TestFoundPaths:
    def test_filters_by_extension_regex_and_hidden(self, tmp_path):
        lines = [".", "./a.py", "./b.md", "./sub/c.py", "./.hidden/d.py"]

        found = _found_paths(lines, tmp_path, r".*\.py$", {"ext": "py"})

        assert found == [tmp_path / "a.py", tmp_path / "sub/c.py"]

    def test_include_and_exclude_globs(self, tmp_path):
        lines = ["./a.py", "./b.md", "./sub/c.py"]

        assert _found_paths(lines, tmp_path, None, {"include": ["c.py"]}) == [
            tmp_path / "sub/c.py"
        ]
        assert _found_paths(lines, tmp_path, None, {"include": ["sub/*"]}) == [
            tmp_path / "sub/c.py"
        ]
        assert _found_paths(lines, tmp_path, None, {"exclude": ["*.md"]}) == [
            tmp_path / "a.py",
            tmp_path / "sub/c.py",
        ]

    def test_hidden_paths_included_when_requested(self, tmp_path):
        assert _found_paths(["./.hidden/d.py"], tmp_path, None, {"hidden": True}) == [
            tmp_path / ".hidden/d.py"
        ]

    def test_root_defaults_to_cwd(self):
        assert _found_paths(["./a.py"], None, None, {}) == [Path.cwd() / "a.py"]

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError, match="invalid pattern"):
            _found_paths([], None, "[unclosed", {})
