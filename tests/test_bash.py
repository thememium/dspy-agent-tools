"""Tests for dspy_agent_tools.bash."""

import os
import sys
import types

import pytest

from dspy_agent_tools.bash import (
    MAX_OUTPUT_BYTES,
    _cap,
    _fd_kwargs,
    _resolve_cwd,
    _resolve_existing,
    _rg_kwargs,
    _run_search,
    _search_text,
    bash_curl,
    bash_exec,
    bash_fetch,
    bash_find,
    bash_find_batch,
    bash_ls,
    bash_ripgrep,
    bash_ripgrep_batch,
)

# --- helpers -------------------------------------------------------------


class TestCap:
    def test_under_cap_returns_output_and_no_notice(self):
        out, notice = _cap("short output")
        assert out == "short output"
        assert notice is None

    def test_over_cap_keeps_head_and_tail(self):
        output = "a" * (MAX_OUTPUT_BYTES + 1)
        out, notice = _cap(output)
        assert notice is not None
        assert "output truncated" in notice
        assert "of 30001 bytes" in out
        assert out.startswith("a" * 20000)
        assert out.rstrip().endswith("a" * 10000)
        assert "[... output truncated:" in out

    def test_over_cap_replaces_invalid_bytes_at_head_boundary(self):
        # A two-byte char straddling the head cut decodes with "replace".
        output = "a" * 19999 + "é" + "b" * 10000
        out, notice = _cap(output)
        assert notice is not None
        assert "\ufffd" in out

    def test_over_cap_replaces_invalid_bytes_at_tail_boundary(self):
        # A two-byte char straddling the tail cut decodes with "replace".
        output = "a" * 20100 + "b" + "é" + "c" * 9999
        assert len(output.encode()) == 30102
        out, notice = _cap(output)
        assert notice is not None
        assert "\ufffd" in out


class TestResolveExisting:
    def test_none_returns_cwd(self):
        assert _resolve_existing(None) == _resolve_existing(None)

    def test_none_resolves_to_current_dir(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert _resolve_existing(None) == tmp_path.resolve()

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            _resolve_existing("/nonexistent/definitely/missing")

    def test_existing_file_is_accepted(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert _resolve_existing(str(f)) == f.resolve()

    def test_tilde_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _resolve_existing("~") == tmp_path.resolve()


class TestResolveCwd:
    def test_missing_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            _resolve_cwd("/nonexistent/definitely/missing")

    def test_file_raises_not_a_directory(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            _resolve_cwd(str(f))

    def test_directory_passes(self, tmp_path):
        assert _resolve_cwd(str(tmp_path)) == tmp_path.resolve()


# --- bash_exec -----------------------------------------------------------


class TestBashExec:
    def test_simple_command(self):
        result = bash_exec("echo hello")
        assert result.startswith("hello\n(")
        assert result.endswith("s)")

    def test_cwd_argument(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here")
        result = bash_exec("cat marker.txt", cwd=str(tmp_path))
        assert result.startswith("here")

    def test_leading_cd_is_used_as_cwd(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here")
        result = bash_exec(f"cd {tmp_path} && cat marker.txt")
        assert result.startswith("here")

    def test_quoted_cd_stays_with_the_shell(self, tmp_path):
        # A quoted path is not captured by _CD_PATTERN; the shell handles it.
        d = tmp_path / "with space"
        d.mkdir()
        (d / "marker.txt").write_text("here")
        result = bash_exec(f'cd "{d}" && cat marker.txt')
        assert result.startswith("here")

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout must be >= 0"):
            bash_exec("echo hi", timeout=-1)

    def test_zero_timeout_disables_deadline(self):
        result = bash_exec("echo zero", timeout=0)
        assert result.startswith("zero")

    def test_timeout_kills_and_reports(self):
        result = bash_exec("echo partial && sleep 30", timeout=1)
        assert result.startswith("partial")
        assert "command timed out after 1s and was killed" in result

    def test_nonzero_exit_is_a_notice(self):
        result = bash_exec("echo out >&2; exit 3")
        assert "[exit code 3]" in result
        assert "out" in result

    def test_large_output_is_truncated(self):
        result = bash_exec("python3 -c \"print('x' * 40000)\"")
        assert "[output truncated to first 20000 and last 10000 bytes]" in result

    def test_wall_time_suffix(self):
        result = bash_exec("true")
        assert result.endswith("s)") and "(" in result


# --- bash_ripgrep --------------------------------------------------------


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


# --- bash_find -----------------------------------------------------------


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


# --- rg option translation ------------------------------------------------


class TestRgOptionParsing:
    def test_long_flag_with_equals_sign(self, tmp_path):
        (tmp_path / "a.py").write_text("alpha = 1\n")
        result = bash_ripgrep("alpha", path=str(tmp_path), options="-n --max-depth=2")
        assert "alpha = 1" in result

    def test_option_missing_value_is_an_error(self, tmp_path):
        result = bash_ripgrep("x", path=str(tmp_path), options="-A")
        assert "[Error] option -A requires a value" in result

    def test_case_sensitive_flag(self, tmp_path):
        (tmp_path / "a.py").write_text("NEEDLE\n")
        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -s")
        assert result.startswith("No matches.")

    def test_smart_case_flag(self, tmp_path):
        (tmp_path / "a.py").write_text("NEEDLE\nneedle\n")
        assert bash_ripgrep("Needle", path=str(tmp_path), options="-n -S").startswith(
            "No matches."
        )
        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -S")
        assert "NEEDLE" in result

    def test_count_flag(self, tmp_path):
        (tmp_path / "a.py").write_text("x\nx\n")
        result = bash_ripgrep("x", path=str(tmp_path), options="-n -c")
        assert result.startswith("2")

    def test_single_u_flag(self, tmp_path):
        (tmp_path / ".gitignore").write_text("ignored_dir/\n")
        (tmp_path / "keep.py").write_text("needle\n")
        ignored = tmp_path / "ignored_dir"
        ignored.mkdir()
        (ignored / "x.py").write_text("needle\n")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "h.py").write_text("needle\n")

        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -u")
        assert "ignored_dir/x.py" in result
        assert ".hidden/h.py" in result

    def test_follow_links_flag(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("needle\n")
        os.symlink(sub, tmp_path / "link")

        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -L")
        assert "link/b.py" in result

    def test_attached_context_value(self, tmp_path):
        (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
        result = bash_ripgrep("two", path=str(tmp_path), options="-n -A3")
        assert "a.py-3-three" in result

    def test_before_and_context_options(self, tmp_path):
        (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
        before = bash_ripgrep("two", path=str(tmp_path), options="-n -B 1")
        assert "a.py-1-one" in before
        both = bash_ripgrep("two", path=str(tmp_path), options="-n -C 1")
        assert "a.py-1-one" in both
        assert "a.py-3-three" in both

    def test_type_not_excludes_extension(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")
        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -T py")
        assert "b.txt" in result
        assert "a.py" not in result

    def test_glob_include_and_exclude(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")
        only_py = bash_ripgrep("needle", path=str(tmp_path), options="-n -g *.py")
        assert "a.py" in only_py
        assert "b.txt" not in only_py
        no_txt = bash_ripgrep("needle", path=str(tmp_path), options="-n -g !*.txt")
        assert "a.py" in no_txt
        assert "b.txt" not in no_txt

    def test_max_depth(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("needle\n")
        result = bash_ripgrep("needle", path=str(tmp_path), options="-n --max-depth 1")
        assert "a.py" in result
        assert "b.py" not in result

    def test_unsupported_value_option(self, tmp_path):
        result = bash_ripgrep("x", path=str(tmp_path), options="-n -j 2")
        assert "[Error] unsupported rg option: -j" in result


# --- fd option translation ------------------------------------------------


class TestFdOptionParsing:
    def test_follow_and_absolute_path(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")
        os.symlink(sub, tmp_path / "link")

        result = bash_find("b", path=str(tmp_path), options="-L --absolute-path")
        assert "link/b.py" in result

    def test_type_files_and_dirs(self, tmp_path):
        (tmp_path / "f.txt").write_text("")
        (tmp_path / "d").mkdir()
        # The trailing "(Ns)" wall-time line is dropped from the split.
        files = bash_find("", path=str(tmp_path), options="-t f").splitlines()[:-1]
        assert files[-1].endswith("f.txt")
        assert not any(line.endswith("/d") for line in files)
        dirs = bash_find("", path=str(tmp_path), options="-t d").splitlines()[:-1]
        assert any(line.endswith("/d") for line in dirs)
        assert not any(line.endswith("f.txt") for line in dirs)

    def test_unsupported_fd_type(self, tmp_path):
        result = bash_find("x", path=str(tmp_path), options="-t l")
        assert "[Error] unsupported fd type: l" in result

    def test_extension_exclude_and_max_depth(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.txt").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.py").write_text("")

        py_only = bash_find("", path=str(tmp_path), options="-e py").splitlines()[:-1]
        assert any(line.endswith("/a.py") for line in py_only)
        assert any(line.endswith("sub/c.py") for line in py_only)
        assert not any(line.endswith("b.txt") for line in py_only)

        no_py = bash_find("", path=str(tmp_path), options="-E *.py").splitlines()[:-1]
        assert any(line.endswith("b.txt") for line in no_py)
        assert not any(line.endswith("a.py") for line in no_py)

        shallow = bash_find("c", path=str(tmp_path), options="-d 1")
        assert "c.py" not in shallow

    def test_unsupported_fd_option(self, tmp_path):
        result = bash_find("x", path=str(tmp_path), options="-j 2")
        assert "[Error] unsupported fd option: -j" in result

    def test_bad_regex_pattern_is_a_notice(self, tmp_path):
        result = bash_find("[unclosed", path=str(tmp_path))
        assert "[Error]" in result


# --- search rendering helpers ----------------------------------------------


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

    def test_large_result_is_truncated_inline(self):
        results = _FakeResults(["x" * (MAX_OUTPUT_BYTES + 1)])
        text = _search_text(results, None)
        assert "[output truncated to first 20000 and last 10000 bytes]" in text


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


# --- bash_curl -----------------------------------------------------------


class TestBashCurl:
    def test_fetches_body(self, tmp_path):
        f = tmp_path / "page.txt"
        f.write_text("plain body")
        result = bash_curl(f"file://{f}")
        assert result.startswith("plain body")

    def test_failed_transfer_is_a_notice(self):
        result = bash_curl("file:///nonexistent/definitely/missing")
        assert "[exit code" in result


# --- bash_fetch ----------------------------------------------------------


HTML_PAGE = """\
<html>
  <head>
    <title>Page Title</title>
    <style>.junk {{ color: red }}</style>
    <script>var junk = 1;</script>
  </head>
  <body>
    <!-- analytics junk comment -->
    <nav>Navigation junk</nav>
    <div class="cookie-banner">Accept cookies</div>
    <article>
      <h1>Real Heading</h1>
      <p>Real paragraph one.</p>
      <p>Real paragraph two.</p>
    </article>
    <footer>Footer junk</footer>
  </body>
</html>
"""


class TestBashFetch:
    def test_converts_html_to_clean_markdown(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(HTML_PAGE)
        result = bash_fetch(f"file://{f}")
        assert result.startswith("# Page Title")
        assert "Real paragraph one." in result
        assert "Navigation junk" not in result
        assert "Accept cookies" not in result
        assert "Footer junk" not in result
        assert "var junk = 1;" not in result
        assert "analytics junk comment" not in result

    def test_failed_transfer_returns_raw_output(self):
        result = bash_fetch("file:///nonexistent/definitely/missing")
        assert "[exit code" in result
        assert "# " not in result.split("\n", 1)[0]

    def test_empty_response(self, tmp_path):
        f = tmp_path / "empty.html"
        f.write_text("")
        result = bash_fetch(f"file://{f}")
        assert "[Error] Empty response from URL" in result

    def test_missing_bs4_reports_import_error(self, tmp_path, monkeypatch):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>hi</p></body></html>")
        monkeypatch.setitem(sys.modules, "bs4", None)
        result = bash_fetch(f"file://{f}")
        assert "bs4 and markdownify are required" in result

    def test_conversion_failure_is_returned_not_raised(self, tmp_path, monkeypatch):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>hi</p></body></html>")

        def boom(*_args, **_kwargs):
            raise RuntimeError("markdownify exploded")

        fake = types.SimpleNamespace(markdownify=boom)
        monkeypatch.setitem(sys.modules, "markdownify", fake)
        result = bash_fetch(f"file://{f}")
        assert "[Error] HTML processing failed: markdownify exploded" in result


# --- bash_ls -------------------------------------------------------------


class TestBashLs:
    def test_lists_entries(self, tmp_path):
        (tmp_path / "visible.txt").write_text("x")
        result = bash_ls(str(tmp_path))
        assert "visible.txt" in result

    def test_custom_options(self, tmp_path):
        (tmp_path / "visible.txt").write_text("x")
        result = bash_ls(str(tmp_path), options="-1")
        assert "visible.txt" in result
        assert "total" not in result

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_ls("/nonexistent/definitely/missing")
