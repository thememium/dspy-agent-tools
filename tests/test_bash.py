"""Tests for dspy_agent_tools.bash."""

import sys
import types

import pytest

from dspy_agent_tools import bash as bash_mod
from dspy_agent_tools.bash import (
    MAX_OUTPUT_BYTES,
    _cap,
    _resolve_cwd,
    _resolve_existing,
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
        assert "[exit code 2]" in result

    def test_missing_path_raises(self):
        with pytest.raises(ValueError, match="Path does not exist"):
            bash_ripgrep("x", path="/nonexistent/definitely/missing")

    def test_single_file_path(self, tmp_path):
        f = tmp_path / "one.txt"
        f.write_text("needle\n")
        result = bash_ripgrep("needle", path=str(f))
        assert "needle" in result


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


# --- _run_batched internals ---------------------------------------------


class TestRunBatchedInternals:
    def test_timeout_kills_batch(self):
        def build(_spec):
            return ["bash", "-c", "echo started && sleep 30"]

        result = bash_mod._run_batched(
            [{"pattern": "slow", "path": "."}], build, timeout=1
        )
        assert result.startswith("== slow in ")
        assert "started" in result
        assert "timed out after 1s and was killed" in result

    def test_zero_timeout_uses_no_deadline(self):
        def build(_spec):
            return ["bash", "-c", "echo quick"]

        result = bash_mod._run_batched(
            [{"pattern": "q", "path": "."}], build, timeout=0
        )
        assert "quick" in result

    def test_exit_code_notice(self):
        def build(_spec):
            return ["bash", "-c", "exit 7"]

        result = bash_mod._run_batched([{"pattern": "x", "path": "."}], build)
        assert "[exit code 7]" in result

    def test_large_batch_output_is_truncated(self):
        def build(_spec):
            return ["bash", "-c", "python3 -c \"print('y' * 40000)\""]

        result = bash_mod._run_batched([{"pattern": "big", "path": "."}], build)
        assert "[output truncated to first 20000 and last 10000 bytes]" in result


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
