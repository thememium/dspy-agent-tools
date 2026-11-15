"""Select the search engine and translate rgapi kwargs to CLI flags."""

import shutil
from pathlib import Path

from ..utils.process import execute
from .output import _found_paths, _has_hidden_component, _sum_counts

_RG_TYPE_EXTS = {
    # rg type names whose canonical extension differs from the name.
    "clojure": "clj",
    "elixir": "ex",
    "erlang": "erl",
    "golang": "go",
    "haskell": "hs",
    "javascript": "js",
    "kotlin": "kt",
    "make": "mk",
    "markdown": "md",
    "perl": "pl",
    "protobuf": "proto",
    "puppet": "pp",
    "python": "py",
    "ruby": "rb",
    "rust": "rs",
    "shell": "sh",
    "typescript": "ts",
    "vimscript": "vim",
}

_NO_VALUE_FLAGS = frozenset(
    {
        # no-ops: line numbers are always reported
        "-n",
        "--line-number",
        "--no-heading",
        "--absolute-path",
        # case handling
        "-i",
        "--ignore-case",
        "-s",
        "--case-sensitive",
        "-S",
        "--smart-case",
        # result shape
        "-l",
        "--files-with-matches",
        "-c",
        "--count",
        # traversal
        "--hidden",
        "--no-ignore",
        "-L",
        "--follow",
        "-u",
        "-uu",
        "-uuu",
    }
)


def _expand_options(options: str) -> list[str]:
    """Split an option string, splitting "--flag=value" into two tokens."""
    tokens: list[str] = []
    for token in options.split():
        if token.startswith("--") and "=" in token:
            tokens.extend(token.split("=", 1))
        else:
            tokens.append(token)
    return tokens


def _take_value(flag: str, tokens: list[str], i: int) -> tuple[str, int]:
    """Return (value, next_index) for an option that needs an argument."""
    if i + 1 >= len(tokens):
        raise ValueError(f"option {flag} requires a value")
    return tokens[i + 1], i + 2


_SEARCH_OK = frozenset({0, 1})  # rg and fd exit 1 when nothing matched

# The kwargs each translator can produce; the CLI engines must be able to
# render every one of them, so unknown keys are an error rather than a silent
# mismatch between backends.
_RG_KEYS = frozenset(
    {
        "case_sensitive",
        "smart_case",
        "paths",
        "count",
        "hidden",
        "ignore",
        "follow_links",
        "after_context",
        "before_context",
        "context",
        "ext",
        "include",
        "exclude",
        "max_depth",
    }
)
_FD_KEYS = frozenset(
    {
        "hidden",
        "ignore",
        "follow_links",
        "files",
        "dirs",
        "ext",
        "exclude",
        "include",
        "max_depth",
    }
)


def _timeout_seconds(timeout_ms: int | None) -> float:
    """Convert rgapi's timeout in milliseconds (None = unlimited) to seconds."""
    return 0 if timeout_ms is None else timeout_ms / 1000


def _engine():
    """Return the search engine: rgapi, else the CLI tools.

    rgapi ships a compiled extension, built from source when no wheel matches
    the interpreter (e.g. Python 3.14), and a broken build raises ImportError.
    Importing here (not at module scope) keeps that failure from taking down
    the whole package, and falling back to the rg/fd binaries, then to POSIX
    grep/find, keeps the search tools working. With none of them available the
    tools report the rgapi failure.
    """
    try:
        import rgapi
    except ImportError as exc:
        return _CliEngine(exc)
    return rgapi


class _CliEngine:
    """rgapi-compatible engine driven by CLI tools, in two tiers.

    Same call shape as rgapi: ``rg(pattern, root, timeout_ms, **kwargs)`` and
    ``fd(pattern=..., root=..., timeout_ms=..., **kwargs)``, with kwargs rendered
    back into the CLI flags they were parsed from.

    ``rg`` prefers the rg binary and falls back to POSIX grep; ``fd`` prefers
    the fd binary and falls back to find. The grep/find tier cannot honour
    .gitignore rules or glob/type filtering the way rg and fd do, so it is a
    last resort rather than an equivalent.
    """

    def __init__(self, reason: ImportError) -> None:
        self.reason = reason
        self._rg = shutil.which("rg")
        self._fd = shutil.which("fd") or shutil.which("fdfind")
        self._grep = shutil.which("grep")
        self._find = shutil.which("find")

    def rg(
        self, pattern: str, root: str, timeout_ms: int | None = None, **kwargs
    ) -> list[str] | int:
        path = Path(root)
        # Run from the root so results are relative to it, like rgapi's.
        cwd, target = (path.parent, path.name) if path.is_file() else (path, ".")
        if self._rg is not None:
            argv = [
                self._rg,
                # rgapi honours .gitignore outside a git repository; rg does not
                # without this (see also fd below).
                "--no-require-git",
                "--no-messages",
                "--no-heading",
                "--color=never",
                "--line-number",
                *_rg_argv(kwargs),
                "--",
                pattern,
                target,
            ]
        elif self._grep is not None:
            argv = [
                self._grep,
                "-nE",
                "-R" if kwargs.get("follow_links") else "-r",
                "-s",
                *_grep_argv(kwargs, pattern),
                "--",
                pattern,
                target,
            ]
        else:
            raise ImportError(
                f"rgapi failed to load ({self.reason}) and neither `rg` nor "
                "`grep` is on PATH; install ripgrep or fix the rgapi extension"
            )
        output, _notices, _elapsed, _code = execute(
            argv, cwd=cwd, timeout=_timeout_seconds(timeout_ms), ok_codes=_SEARCH_OK
        )
        # rgapi reports paths relative to the root ("a.py"), rg prints "./a.py".
        lines = [line.removeprefix("./") for line in output.splitlines()]
        if self._rg is None:  # grep tier: match rg's output shape
            lines = [line for line in lines if line != "--"]  # stray group separator
            if not kwargs.get("hidden") and not path.is_file():
                lines = [line for line in lines if not _has_hidden_component(line)]
        if kwargs.get("count"):
            return _sum_counts(lines)
        if kwargs.get("paths"):  # rgapi's files-with-matches are absolute
            base = path.parent if path.is_file() else path
            return [str(base / line) for line in lines]
        return lines

    def fd(
        self,
        pattern: str | None = None,
        root: Path | str | None = None,
        timeout_ms: int | None = None,
        **kwargs,
    ) -> list[Path]:
        if self._fd is not None:
            argv = [
                self._fd,
                "--no-require-git",  # rgapi honours .gitignore outside a repo; fd does not
                "--color=never",
                "--absolute-path",
                *_fd_argv(kwargs),
            ]
            if pattern is not None:
                argv += ["--", pattern]
        elif self._find is not None:
            argv = _find_argv(self._find, kwargs) + ["-print"]
        else:
            raise ImportError(
                f"rgapi failed to load ({self.reason}) and neither `fd` nor "
                "`find` is on PATH; install fd or fix the rgapi extension"
            )
        output, _notices, _elapsed, _code = execute(
            argv,
            cwd=None if root is None else Path(root),
            timeout=_timeout_seconds(timeout_ms),
            ok_codes=_SEARCH_OK,
        )
        if self._fd is not None:
            return [Path(line) for line in output.splitlines()]
        return _found_paths(output.splitlines(), root, pattern, kwargs)


def _rg_argv(kwargs: dict) -> list[str]:
    """Render rgapi.rg keyword arguments as rg CLI flags."""
    unknown = sorted(set(kwargs) - _RG_KEYS)
    if unknown:  # a new rgapi kwarg needs its flag here; fail loudly, not silently
        raise ValueError(f"unsupported rg option: {unknown[0]}")
    argv: list[str] = []
    if kwargs.get("case_sensitive") is False:
        argv.append("--ignore-case")
    elif kwargs.get("case_sensitive"):
        argv.append("--case-sensitive")
    if kwargs.get("smart_case"):
        argv.append("--smart-case")
    if kwargs.get("paths"):
        argv.append("--files-with-matches")
    if kwargs.get("count"):
        argv.append("--count")
    if kwargs.get("hidden"):
        argv.append("--hidden")
    if kwargs.get("ignore") is False:
        argv.append("--no-ignore")
    if kwargs.get("follow_links"):
        argv.append("--follow")
    for key, flag in (
        ("after_context", "-A"),
        ("before_context", "-B"),
        ("context", "-C"),
    ):
        if key in kwargs:
            argv += [flag, str(kwargs[key])]
    if "ext" in kwargs:  # rgapi's ext is an extension; rg filters by glob
        argv += ["--glob", f"*.{kwargs['ext']}"]
    for glob in kwargs.get("include", []):
        argv += ["--glob", glob]
    for glob in kwargs.get("exclude", []):
        argv += ["--glob", f"!{glob}"]
    if "max_depth" in kwargs:
        argv += ["--max-depth", str(kwargs["max_depth"])]
    return argv


def _fd_argv(kwargs: dict) -> list[str]:
    """Render rgapi.fd keyword arguments as fd CLI flags."""
    unknown = sorted(set(kwargs) - _FD_KEYS)
    if unknown:
        raise ValueError(f"unsupported fd option: {unknown[0]}")
    argv: list[str] = []
    if kwargs.get("hidden"):
        argv.append("-H")
    if kwargs.get("ignore") is False:
        argv.append("-I")
    if kwargs.get("follow_links"):
        argv.append("-L")
    if kwargs.get("dirs") and not kwargs.get("files"):
        argv += ["-t", "d"]
    elif not kwargs.get("dirs"):
        argv += ["-t", "f"]  # rgapi.fd lists files by default
    if "ext" in kwargs:
        argv += ["-e", kwargs["ext"]]
    for glob in kwargs.get("include", []):
        argv += ["-g", glob]
    for glob in kwargs.get("exclude", []):
        argv += ["-E", glob]
    if "max_depth" in kwargs:
        argv += ["-d", str(kwargs["max_depth"])]
    return argv


def _grep_argv(kwargs: dict, pattern: str) -> list[str]:
    """Render rgapi.rg keyword arguments as grep flags.

    grep reads no ignore files, so this tier only skips the VCS directories
    rgapi skips by default, plus hidden paths unless they were requested.
    """
    unknown = sorted(set(kwargs) - _RG_KEYS)
    if unknown:
        raise ValueError(f"unsupported rg option: {unknown[0]}")
    if "max_depth" in kwargs:
        raise ValueError("max-depth needs rg or fd; grep cannot limit traversal")
    argv: list[str] = []
    if kwargs.get("case_sensitive") is False:
        argv.append("-i")
    elif kwargs.get("smart_case") and not any(c.isupper() for c in pattern):
        argv.append("-i")  # grep has no smart case; mimic it from the pattern
    if kwargs.get("paths"):
        argv.append("-l")
    if kwargs.get("count"):
        argv.append("-c")
    # Hidden paths are filtered from the output: BSD grep's --exclude globs are
    # matched against the full "./path", where ".*" also matches the "." dir.
    if kwargs.get("ignore") is not False:
        argv += [f"--exclude-dir={name}" for name in (".git", ".hg", ".svn")]
    for key, flag in (
        ("after_context", "-A"),
        ("before_context", "-B"),
        ("context", "-C"),
    ):
        if key in kwargs:
            argv += [flag, str(kwargs[key])]
    if "ext" in kwargs:
        argv.append(f"--include=*.{kwargs['ext']}")
    for glob in kwargs.get("include", []):
        argv.append(f"--include={glob}")
    for glob in kwargs.get("exclude", []):
        argv.append(f"--exclude={glob}")
    return argv


def _find_argv(binary: str, kwargs: dict) -> list[str]:
    """Render rgapi.fd keyword arguments as a find expression."""
    unknown = sorted(set(kwargs) - _FD_KEYS)
    if unknown:
        raise ValueError(f"unsupported fd option: {unknown[0]}")
    argv = [binary]
    if kwargs.get("follow_links"):
        argv.append("-L")
    argv.append(".")
    if "max_depth" in kwargs:
        argv += ["-maxdepth", str(kwargs["max_depth"])]
    if kwargs.get("dirs") and not kwargs.get("files"):
        argv += ["-type", "d"]
    elif not kwargs.get("dirs"):
        argv += ["-type", "f"]  # rgapi.fd lists files by default
    if kwargs.get("ignore") is not False:
        for name in (".git", ".hg", ".svn"):
            argv += ["-not", "-path", f"./{name}", "-not", "-path", f"./{name}/*"]
    return argv


def _rg_kwargs(options: str, pattern: str) -> tuple[str, dict]:
    """Translate rg flags into rgapi.rg keyword arguments.

    Returns (pattern, kwargs); pattern is passed through. Raises ValueError
    for flags with no rgapi equivalent so the caller can surface a notice
    instead of silently ignoring the request.
    """
    kwargs: dict = {}
    tokens = _expand_options(options)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in _NO_VALUE_FLAGS:
            if tok in ("-i", "--ignore-case"):
                kwargs["case_sensitive"] = False
            elif tok in ("-s", "--case-sensitive"):
                kwargs["case_sensitive"] = True
            elif tok in ("-S", "--smart-case"):
                kwargs["smart_case"] = True
            elif tok in ("-l", "--files-with-matches"):
                kwargs["paths"] = True
            elif tok in ("-c", "--count"):
                kwargs["count"] = True
            elif tok in ("--hidden",):
                kwargs["hidden"] = True
            elif tok in ("--no-ignore", "-u", "-uu", "-uuu"):
                kwargs["ignore"] = False
                if tok == "-u":
                    kwargs["hidden"] = True
            elif tok in ("-L", "--follow"):
                kwargs["follow_links"] = True
            continue
        if tok in (
            "-w",
            "--word-regexp",
            "-x",
            "--line-regexp",
            "-F",
            "--fixed-strings",
        ):
            raise ValueError(f"unsupported rg option: {tok}")
        if tok.startswith("--") or len(tok) == 2:
            flag = tok
            value, i = _take_value(tok, tokens, i - 1)
        else:
            flag, value = tok[:2], tok[2:]  # attached value like "-A3" or "-tpy"
        if flag in ("-A", "--after-context"):
            kwargs["after_context"] = int(value)
        elif flag in ("-B", "--before-context"):
            kwargs["before_context"] = int(value)
        elif flag in ("-C", "--context"):
            kwargs["context"] = int(value)
        elif flag in ("-t", "--type"):
            kwargs["ext"] = _RG_TYPE_EXTS.get(value, value)
        elif flag in ("-T", "--type-not"):
            kwargs.setdefault("exclude", []).append(
                f"*.{_RG_TYPE_EXTS.get(value, value)}"
            )
        elif flag in ("-g", "--glob"):
            if value.startswith("!"):
                kwargs.setdefault("exclude", []).append(value[1:])
            else:
                kwargs.setdefault("include", []).append(value)
        elif flag == "--max-depth":
            kwargs["max_depth"] = int(value)
        else:
            raise ValueError(f"unsupported rg option: {tok}")
    return pattern, kwargs


def _fd_kwargs(
    options: str, pattern: str | None, glob: bool = False
) -> tuple[str | None, dict]:
    """Translate fd flags into rgapi.fd keyword arguments.

    Returns (pattern, kwargs). With `glob=True` or the "--glob" flag the
    pattern is reinterpreted as an include glob; otherwise it stays a
    regex matched against file names. Raises ValueError for flags with no
    rgapi equivalent.
    """
    glob_mode = glob
    kwargs: dict = {}
    tokens = _expand_options(options)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok in ("-H", "--hidden"):
            kwargs["hidden"] = True
        elif tok in ("-I", "--no-ignore", "-u"):
            kwargs["ignore"] = False
        elif tok in ("-L", "--follow"):
            kwargs["follow_links"] = True
        elif tok == "--absolute-path":
            continue
        elif tok in ("-g", "--glob"):
            glob_mode = True
        else:
            value, i = _take_value(tok, tokens, i - 1)
            if tok in ("-t", "--type"):
                if value == "f":
                    kwargs["files"], kwargs["dirs"] = True, False
                elif value == "d":
                    kwargs["files"], kwargs["dirs"] = False, True
                else:
                    raise ValueError(f"unsupported fd type: {value}")
            elif tok in ("-e", "--extension"):
                kwargs["ext"] = value
            elif tok in ("-E", "--exclude"):
                kwargs.setdefault("exclude", []).append(value)
            elif tok in ("-d", "--max-depth"):
                kwargs["max_depth"] = int(value)
            else:
                raise ValueError(f"unsupported fd option: {tok}")
    if glob_mode:
        kwargs.setdefault("include", []).append(pattern or "**")
        pattern = None
    return pattern, kwargs
