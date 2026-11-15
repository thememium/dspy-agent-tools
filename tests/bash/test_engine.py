"""Tests for dspy_agent_tools.bash.engine."""

import os
import shlex
import sys

import pytest

from dspy_agent_tools.bash import bash_find, bash_ripgrep
from dspy_agent_tools.bash.engine import (
    _fd_argv,
    _find_argv,
    _grep_argv,
    _rg_argv,
    _timeout_seconds,
)


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
        assert ".hidden/h.py" not in result
        for options in ("-n -uu", "-n -uuu"):
            assert ".hidden/h.py" in bash_ripgrep(
                "needle", path=str(tmp_path), options=options
            )

    def test_quoted_globs(self, tmp_path):
        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")
        result = bash_ripgrep("needle", path=str(tmp_path), options="-g '*.py'")
        assert "a.py" in result
        assert "b.txt" not in result
        excluded = bash_ripgrep("needle", path=str(tmp_path), options="-g '!*.py'")
        assert "a.py" not in excluded
        assert "b.txt" in excluded

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


class TestRgArgv:
    def test_every_flag(self):
        assert _rg_argv(
            {
                "case_sensitive": False,
                "smart_case": True,
                "paths": True,
                "count": True,
                "hidden": True,
                "ignore": False,
                "follow_links": True,
                "after_context": 1,
                "before_context": 2,
                "context": 3,
                "ext": "py",
                "include": ["*.md"],
                "exclude": ["*.txt"],
                "max_depth": 4,
            }
        ) == [
            "--ignore-case",
            "--smart-case",
            "--files-with-matches",
            "--count",
            "--hidden",
            "--no-ignore",
            "--follow",
            "-A",
            "1",
            "-B",
            "2",
            "-C",
            "3",
            "--glob",
            "*.py",
            "--glob",
            "*.md",
            "--glob",
            "!*.txt",
            "--max-depth",
            "4",
        ]

    def test_case_sensitive(self):
        assert _rg_argv({"case_sensitive": True}) == ["--case-sensitive"]

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unsupported rg option: bogus"):
            _rg_argv({"bogus": True})


class TestFdArgv:
    def test_every_flag(self):
        assert _fd_argv(
            {
                "hidden": True,
                "ignore": False,
                "follow_links": True,
                "dirs": True,
                "ext": "py",
                "include": ["*.md"],
                "exclude": ["*.txt"],
                "max_depth": 3,
            }
        ) == [
            "-H",
            "-I",
            "-L",
            "-t",
            "d",
            "-e",
            "py",
            "-g",
            "*.md",
            "-E",
            "*.txt",
            "-d",
            "3",
        ]

    def test_files_and_dirs_together_omit_the_type_flag(self):
        assert _fd_argv({"dirs": True, "files": True}) == []

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unsupported fd option: bogus"):
            _fd_argv({"bogus": True})


class TestGrepArgv:
    def test_every_flag(self):
        assert _grep_argv(
            {
                "case_sensitive": False,
                "smart_case": True,
                "paths": True,
                "count": True,
                "ignore": False,
                "after_context": 1,
                "before_context": 2,
                "context": 3,
                "ext": "py",
                "include": ["*.md"],
                "exclude": ["*.txt"],
            },
            "needle",
        ) == [
            "-i",
            "-l",
            "-c",
            "-A",
            "1",
            "-B",
            "2",
            "-C",
            "3",
            "--include=*.py",
            "--include=*.md",
            "--exclude=*.txt",
        ]

    def test_smart_case_mimics_case_insensitivity_for_lowercase_patterns(self):
        assert _grep_argv({"smart_case": True}, "needle") == [
            "-i",
            "--exclude-dir=.git",
            "--exclude-dir=.hg",
            "--exclude-dir=.svn",
        ]

    def test_smart_case_stays_case_sensitive_for_uppercase_patterns(self):
        assert "-i" not in _grep_argv({"smart_case": True}, "Needle")

    def test_max_depth_is_rejected(self):
        with pytest.raises(ValueError, match="max-depth needs rg or fd"):
            _grep_argv({"max_depth": 2}, "needle")

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unsupported rg option: bogus"):
            _grep_argv({"bogus": True}, "needle")


class TestFindArgv:
    def test_every_flag(self):
        assert _find_argv(
            "find", {"follow_links": True, "max_depth": 2, "dirs": True}
        ) == [
            "find",
            "-L",
            ".",
            "-maxdepth",
            "2",
            "-type",
            "d",
            "-not",
            "-path",
            "./.git",
            "-not",
            "-path",
            "./.git/*",
            "-not",
            "-path",
            "./.hg",
            "-not",
            "-path",
            "./.hg/*",
            "-not",
            "-path",
            "./.svn",
            "-not",
            "-path",
            "./.svn/*",
        ]

    def test_no_ignore_drops_vcs_excludes(self):
        assert _find_argv("find", {"ignore": False}) == ["find", ".", "-type", "f"]

    def test_files_and_dirs_together_omit_the_type_flag(self):
        assert _find_argv("find", {"dirs": True, "files": True, "ignore": False}) == [
            "find",
            ".",
        ]

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unsupported fd option: bogus"):
            _find_argv("find", {"bogus": True})


class TestSearchHelpers:
    def test_timeout_seconds(self):
        assert _timeout_seconds(None) == 0
        assert _timeout_seconds(1500) == 1.5


def _fake_binary(directory, name, argv_file, output, exit_code=0):
    """Write an executable that records its argv and prints `output`."""
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_file))}\n"
        f"printf '%s' {shlex.quote(output)}\n"
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)


class TestCliFallback:
    @pytest.mark.parametrize(
        "binary, search", [("rg", bash_ripgrep), ("fd", bash_find)]
    )
    def test_backend_errors_are_not_reported_as_results(
        self, tmp_path, monkeypatch, binary, search
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(bin_dir, binary, tmp_path / "argv", "invalid regex", exit_code=2)
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)
        result = search("[", path=str(tmp_path))
        assert result.startswith("[Error]")
        assert "invalid regex" in result
        assert "[exit code 2]" in result

    def test_rg_binary_runs_when_rgapi_cannot_load(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_file = tmp_path / "argv"
        _fake_binary(bin_dir, "rg", argv_file, "a.py:1:needle\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path), options="-i -g !*.md -t py")

        assert "a.py:1:needle" in result
        argv = argv_file.read_text().splitlines()
        for flag in ("--ignore-case", "--glob", "*.py", "!*.md", "--no-heading"):
            assert flag in argv
        assert argv[-2:] == ["needle", "."]

    def test_rg_no_match_exit_code_is_not_an_error(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(bin_dir, "rg", tmp_path / "argv", "", exit_code=1)
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path))

        assert result.startswith("No matches.")
        assert "[exit code" not in result

    def test_rg_count_sums_per_file_counts(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(bin_dir, "rg", tmp_path / "argv", "a.py:2\nb.py:3\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path), options="-c")

        assert result.splitlines()[0] == "5"

    def test_fd_binary_runs_when_rgapi_cannot_load(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_file = tmp_path / "argv"
        _fake_binary(bin_dir, "fd", argv_file, f"{tmp_path}/a.py\n{tmp_path}/b.py\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_find("a", path=str(tmp_path), options="-e py")

        assert "a.py" in result
        argv = argv_file.read_text().splitlines()
        assert "--absolute-path" in argv
        assert argv[argv.index("-e") : argv.index("-e") + 2] == ["-e", "py"]
        assert argv[argv.index("-t") : argv.index("-t") + 2] == ["-t", "f"]

    def test_grep_runs_when_rg_is_absent(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_file = tmp_path / "argv"
        _fake_binary(bin_dir, "grep", argv_file, "./a.py:1:needle one\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path), options="-i -t py")

        assert "a.py:1:needle one" in result
        argv = argv_file.read_text().splitlines()
        for flag in (
            "-nE",
            "-r",
            "-s",
            "-i",
            "--include=*.py",
            "--exclude-dir=.git",
        ):
            assert flag in argv
        assert argv[-2:] == ["needle", "."]

    def test_grep_hidden_paths_are_skipped_unless_requested(
        self, tmp_path, monkeypatch
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(
            bin_dir, "grep", tmp_path / "argv", "./.hidden/d.py:1:x\n./a.py:1:y\n"
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        default = bash_ripgrep("needle", path=str(tmp_path))
        hidden = bash_ripgrep("needle", path=str(tmp_path), options="-n --hidden")

        assert ".hidden" not in default
        assert "a.py:1:y" in default
        assert ".hidden/d.py:1:x" in hidden

    def test_grep_count_is_summed(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(bin_dir, "grep", tmp_path / "argv", "./a.py:2\n./b.py:3\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path), options="-c")

        assert result.splitlines()[0] == "5"

    def test_find_runs_when_fd_is_absent(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_file = tmp_path / "argv"
        _fake_binary(
            bin_dir,
            "find",
            argv_file,
            "./a.py\n./sub\n./sub/b.py\n./.hidden/c.py\n",
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_find(r".*\.py$", path=str(tmp_path), options="-d 2")

        found = [line for line in result.splitlines() if line.startswith("/")]
        assert found == [str(tmp_path / "a.py"), str(tmp_path / "sub/b.py")]
        argv = argv_file.read_text().splitlines()
        assert argv[argv.index("-maxdepth") : argv.index("-maxdepth") + 2] == [
            "-maxdepth",
            "2",
        ]
        assert argv[argv.index("-type") : argv.index("-type") + 2] == ["-type", "f"]
        assert argv[argv.index("-not") : argv.index("-not") + 2] == ["-not", "-path"]

        hidden = bash_find(r".*\.py$", path=str(tmp_path), options="--hidden")
        assert str(tmp_path / ".hidden/c.py") in hidden

    def test_grep_files_with_matches_are_absolute(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_binary(bin_dir, "grep", tmp_path / "argv", "./a.py\n./sub/b.py\n")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_ripgrep("needle", path=str(tmp_path), options="-n -l")

        assert result.splitlines()[:2] == [
            str(tmp_path / "a.py"),
            str(tmp_path / "sub/b.py"),
        ]

    def test_grep_follow_uses_recursive_flag(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        argv_file = tmp_path / "argv"
        _fake_binary(bin_dir, "grep", argv_file, "")
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        bash_ripgrep("needle", path=str(tmp_path), options="-n -L")

        assert "-R" in argv_file.read_text().splitlines()

    def test_no_find_engine_is_an_error_section(self, tmp_path, monkeypatch):
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setitem(sys.modules, "rgapi", None)

        result = bash_find("x", path=str(tmp_path))

        assert "[Error]" in result
        assert "neither `fd` nor `find`" in result
