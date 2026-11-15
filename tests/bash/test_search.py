"""Tests for dspy_agent_tools.bash.search."""

import sys

import pytest

from dspy_agent_tools.bash import (
    bash_find,
    bash_find_batch,
    bash_glob,
    bash_glob_batch,
    bash_ripgrep,
    bash_ripgrep_batch,
)
from dspy_agent_tools.bash.engine import _fd_kwargs, _rg_kwargs
from dspy_agent_tools.bash.search import (
    _run_search,
    _search_text,
)


class TestBashRipgrep:
    def test_match(self, tmp_path):
        (tmp_path / "a.py").write_text("target_line = 1\n")
        result = bash_ripgrep("target_line", path=str(tmp_path))
        assert "target_line = 1" in result
        assert "a.py:1" in result

    def test_no_matches(self, tmp_path):
        result = bash_ripgrep("nothing_here_xyz", path=str(tmp_path))
        assert result.startswith("No matches.")

    def test_bad_regex_is_a_notice_not_a_raise(self, tmp_path):
        result = bash_ripgrep("[unclosed", path=str(tmp_path))
        assert "[Error]" in result

    def test_no_engine_at_all_is_an_error_not_a_raise(self, tmp_path, monkeypatch):
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        (tmp_path / "a.py").write_text("needle\n")
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setitem(sys.modules, "rgapi", None)
        result = bash_ripgrep("needle", path=str(tmp_path))
        assert "[Error]" in result
        assert "rgapi failed to load" in result
        assert "neither `rg` nor `grep`" in result

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_ripgrep("x", path="/nonexistent/definitely/missing")

    def test_single_file_path(self, tmp_path):
        f = tmp_path / "one.txt"
        f.write_text("needle\n")
        result = bash_ripgrep("needle", path=str(f))
        assert "needle" in result

    def test_gitignore_and_hidden_are_respected(self, tmp_path):
        (tmp_path / ".gitignore").write_text("ignored_dir/\n")
        (tmp_path / "kept.py").write_text("needle\n")
        ignored = tmp_path / "ignored_dir"
        ignored.mkdir()
        (ignored / "x.py").write_text("needle\n")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "h.py").write_text("needle\n")

        default = bash_ripgrep("needle", path=str(tmp_path))
        assert "kept.py" in default
        assert "ignored_dir" not in default
        assert ".hidden" not in default

        everything = bash_ripgrep(
            "needle", path=str(tmp_path), options="-n --no-ignore --hidden"
        )
        assert "ignored_dir/x.py" in everything
        assert ".hidden/h.py" in everything

    def test_case_insensitive_and_type_options(self, tmp_path):
        (tmp_path / "a.py").write_text("NEEDLE = 1\n")
        (tmp_path / "b.txt").write_text("needle = 2\n")
        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -i -t py")
        assert "a.py" in result
        assert "b.txt" not in result

    def test_files_with_matches_and_context_options(self, tmp_path):
        (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
        assert bash_ripgrep("two", path=str(tmp_path), options="-n -l") == (
            str(tmp_path / "a.py") + "\n(0.0s)"
        )
        context = bash_ripgrep("two", path=str(tmp_path), options="-n -A 1")
        assert "a.py-3-three" in context

    def test_unsupported_option_is_a_notice(self, tmp_path):
        result = bash_ripgrep("x", path=str(tmp_path), options="-n -w")
        assert "[Error]" in result
        assert "unsupported rg option" in result


class TestBashRipgrepBatch:
    def test_two_searches(self, tmp_path):
        (tmp_path / "a.py").write_text("alpha = 1\nbeta = 2\n")
        result = bash_ripgrep_batch(
            [
                {"pattern": "alpha", "path": str(tmp_path)},
                {"pattern": "beta", "path": str(tmp_path)},
            ]
        )
        assert f"== alpha in {tmp_path.resolve()} ==" in result
        assert "alpha = 1" in result
        assert "beta = 2" in result

    def test_no_match_section(self, tmp_path):
        result = bash_ripgrep_batch([{"pattern": "nope_xyz", "path": str(tmp_path)}])
        assert "No matches." in result

    def test_missing_pattern_key_is_an_error_section(self, tmp_path):
        result = bash_ripgrep_batch([{"path": str(tmp_path)}])
        assert "Error:" in result

    def test_missing_path_is_an_error_section(self):
        result = bash_ripgrep_batch(
            [{"pattern": "x", "path": "/nonexistent/definitely/missing"}]
        )
        assert "Error: Path does not exist" in result

    def test_default_path_is_dot(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.py").write_text("alpha = 1\n")
        result = bash_ripgrep_batch([{"pattern": "alpha"}])
        assert "alpha = 1" in result


class TestBashFind:
    def test_pattern_match(self, tmp_path):
        (tmp_path / "target_file.py").write_text("")
        result = bash_find("target_file", path=str(tmp_path))
        assert "target_file.py" in result

    def test_no_pattern_lists_everything(self, tmp_path):
        (tmp_path / "some.txt").write_text("")
        result = bash_find(path=str(tmp_path))
        assert "some.txt" in result

    def test_glob_option(self, tmp_path):
        (tmp_path / "keep.py").write_text("")
        (tmp_path / "skip.txt").write_text("")
        result = bash_find("*.py", path=str(tmp_path), options="--glob")
        assert "keep.py" in result
        assert "skip.txt" not in result

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_find("x", path="/nonexistent/definitely/missing")

    def test_no_matches_returns_empty_output(self, tmp_path):
        result = bash_find("zzz_no_match_zzz", path=str(tmp_path))
        assert result.strip() == "(0.0s)"

    def test_gitignore_and_hidden_are_respected(self, tmp_path):
        (tmp_path / ".gitignore").write_text("ignored_dir/\n")
        (tmp_path / "kept.py").write_text("")
        ignored = tmp_path / "ignored_dir"
        ignored.mkdir()
        (ignored / "skip.py").write_text("")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "h.py").write_text("")

        default = bash_find("py", path=str(tmp_path))
        assert "kept.py" in default
        assert "ignored_dir" not in default
        assert ".hidden" not in default

        everything = bash_find("py", path=str(tmp_path), options="--hidden --no-ignore")
        assert "ignored_dir/skip.py" in everything
        assert ".hidden/h.py" in everything


class TestBashFindBatch:
    def test_two_lookups(self, tmp_path):
        (tmp_path / "one_target.py").write_text("")
        (tmp_path / "two_target.py").write_text("")
        result = bash_find_batch(
            [
                {"pattern": "one_target", "path": str(tmp_path)},
                {"pattern": "two_target", "path": str(tmp_path)},
            ]
        )
        assert f"== one_target in {tmp_path.resolve()} ==" in result
        assert "one_target.py" in result
        assert "two_target.py" in result

    def test_missing_path_is_an_error_section(self):
        result = bash_find_batch(
            [{"pattern": "x", "path": "/nonexistent/definitely/missing"}]
        )
        assert "Error: Path does not exist" in result

    def test_empty_pattern_lists_all(self, tmp_path):
        (tmp_path / "listed.txt").write_text("")
        result = bash_find_batch([{"path": str(tmp_path)}])
        assert "listed.txt" in result


class _FakeResults(list):
    stop_reason = None


class TestSearchText:
    def test_timeout_notice(self):
        results = _FakeResults(["a.py:1:needle"])
        results.stop_reason = "timeout"
        assert "[search timed out; partial results shown]" in _search_text(
            results, None
        )

    def test_count_result(self):
        assert _search_text(3, None) == "3"

    def test_no_match_replacement(self):
        assert _search_text(_FakeResults(), "No matches.") == "No matches."

    def test_large_result_is_returned_in_full(self):
        line = "x" * 100_000
        assert _search_text(_FakeResults([line]), None) == line


class TestRunSearch:
    def test_option_parse_error_becomes_error_body(self):
        body = _run_search(
            {"pattern": "x", "options": "-j 2"}, "-n", _rg_kwargs, lambda p, k: None
        )
        assert body == "[Error] unsupported rg option: -j"

    def test_run_error_becomes_error_body(self):
        def boom(_pat, _kwargs):
            raise ValueError("regex exploded")

        body = _run_search({"pattern": "["}, "-n", _rg_kwargs, boom)
        assert body == "[Error] regex exploded"

    def test_missing_pattern_raises_keyerror(self):
        with pytest.raises(KeyError, match="pattern"):
            _run_search({}, "-n", _rg_kwargs, lambda p, k: None)

    def test_pattern_optional_when_not_required(self):
        body = _run_search(
            {}, "", _fd_kwargs, lambda _pat, _kwargs: _FakeResults(), False, None
        )
        assert body == ""

    def test_successful_search(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")

        def run(pat, kwargs):
            return _FakeResults([f"a.py:1:{pat}"])

        body = _run_search({"pattern": "needle"}, "-n", _rg_kwargs, run)
        assert body == "a.py:1:needle"


class TestBashGlob:
    def test_bare_glob_matches_any_depth(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")
        (tmp_path / "c.md").write_text("")
        result = bash_glob("*.py", path=str(tmp_path))
        lines = result.splitlines()[:-1]
        assert sorted(lines) == sorted([str(tmp_path / "a.py"), str(sub / "b.py")])

    def test_anchored_glob_with_separator(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")
        result = bash_glob("sub/*.py", path=str(tmp_path))
        assert str(sub / "b.py") in result
        assert str(tmp_path / "a.py") not in result

    def test_star_does_not_cross_separator(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")
        assert "b.py" not in bash_glob("*.py", path=str(tmp_path), options="-d 1")

    def test_output_is_sorted(self, tmp_path):
        for name in ("c.py", "a.py", "b.py"):
            (tmp_path / name).write_text("")
        lines = bash_glob("*.py", path=str(tmp_path)).splitlines()[:-1]
        assert lines == sorted(lines)

    def test_type_files_only(self, tmp_path):
        (tmp_path / "f.py").write_text("")
        (tmp_path / "d.py").mkdir()
        result = bash_glob("*.py", path=str(tmp_path), options="-t f")
        assert "f.py" in result
        assert not result.rstrip().endswith("/d.py")

    def test_gitignore_and_hidden_are_respected(self, tmp_path):
        (tmp_path / ".gitignore").write_text("ignored_dir/\n")
        (tmp_path / "kept.py").write_text("")
        ignored = tmp_path / "ignored_dir"
        ignored.mkdir()
        (ignored / "skip.py").write_text("")
        hidden = tmp_path / ".hiddendir"
        hidden.mkdir()
        (hidden / "h.py").write_text("")

        default = bash_glob("*.py", path=str(tmp_path))
        assert "kept.py" in default
        assert "ignored_dir" not in default
        assert ".hiddendir" not in default

        everything = bash_glob(
            "*.py", path=str(tmp_path), options="--hidden --no-ignore"
        )
        assert "ignored_dir/skip.py" in everything
        assert ".hiddendir/h.py" in everything

    def test_no_matches_returns_empty_output(self, tmp_path):
        result = bash_glob("zzz_none_zzz.py", path=str(tmp_path))
        assert result.strip() == "(0.0s)"

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_glob("*.py", path="/nonexistent/definitely/missing")

    def test_unsupported_option_is_a_notice(self, tmp_path):
        result = bash_glob("*.py", path=str(tmp_path), options="-j 2")
        assert "[Error] unsupported fd option: -j" in result


class TestBashGlobBatch:
    def test_two_lookups_sorted_sections(self, tmp_path):
        (tmp_path / "one.py").write_text("")
        (tmp_path / "two.py").write_text("")
        result = bash_glob_batch(
            [
                {"pattern": "two.py", "path": str(tmp_path)},
                {"pattern": "one.py", "path": str(tmp_path)},
            ]
        )
        assert f"== two.py in {tmp_path.resolve()} ==" in result
        assert f"== one.py in {tmp_path.resolve()} ==" in result
        # Sections hold only their own match, in sorted order.
        one_section = result.split("== one.py in")[1]
        assert one_section.rstrip().endswith("one.py\n(0.0s)")
        assert "two.py" not in one_section

    def test_missing_path_is_an_error_section(self):
        result = bash_glob_batch(
            [{"pattern": "*.py", "path": "/nonexistent/definitely/missing"}]
        )
        assert "Error: Path does not exist" in result

    def test_no_engine_at_all_is_an_error_section(self, tmp_path, monkeypatch):
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setitem(sys.modules, "rgapi", None)
        result = bash_glob_batch([{"pattern": "*.py", "path": str(tmp_path)}])
        assert "[Error]" in result
        assert "rgapi failed to load" in result

    def test_empty_pattern_lists_all(self, tmp_path):
        (tmp_path / "listed.txt").write_text("")
        result = bash_glob_batch([{"path": str(tmp_path)}])
        assert "listed.txt" in result
