"""Search and path-lookup tools built on rgapi or the CLI fallbacks."""

import time

from ..utils.process import DEFAULT_TIMEOUT, render, resolve_existing
from .engine import _engine, _fd_kwargs, _rg_kwargs


def _search_text(result, no_match: str | None) -> str:
    """Render an rgapi result with timeout notices, without wall time."""
    notices: list[str] = []
    if getattr(result, "stop_reason", None) == "timeout":
        notices.append("[search timed out; partial results shown]")
    if isinstance(result, int):
        text = str(result)  # rg count=True returns a bare count
    else:
        text = "\n".join(str(item) for item in result)
    if no_match is not None and not text.strip():
        text = no_match
    if notices:
        text = text.rstrip("\n") + "\n\n" + "\n".join(notices)
    return text.rstrip("\n")


def _search_error(exc: Exception) -> str:
    """Render a failed search as an "[Error]" body instead of raising."""
    return f"[Error] {exc}"


def _run_search(
    spec: dict[str, str],
    default_options: str,
    parse_kwargs,
    run,
    require_pattern: bool = True,
    no_match: str | None = "No matches.",
) -> str:
    """Run one search spec through rgapi and render its section body.

    Raises KeyError/ValueError/OSError for the batch caller to turn into
    an Error section; bad regexes and unknown flags are caught here and
    returned as an "[Error]" body instead.
    """
    if require_pattern and "pattern" not in spec:
        raise KeyError("pattern")
    try:
        pattern, kwargs = parse_kwargs(
            spec.get("options", default_options) or "", spec.get("pattern", "")
        )
    except ValueError as exc:
        return _search_error(exc)
    try:
        result = run(pattern, kwargs)
    except (ValueError, ImportError) as exc:  # bad regex, unreadable root, no rgapi
        return _search_error(exc)
    return _search_text(result, no_match)


def bash_ripgrep(pattern: str, path: str = ".", options: str = "-n") -> str:
    """
    Search file contents with ripgrep-style matching and return the lines.

    WHAT: Searches files with the rgapi library (ripgrep's matching engine
    as a Python module, no rg binary needed) and returns lines matching a
    regex, prefixed with "file:line:". WHEN: The default tool for finding
    code, text, symbols, or config inside files. Use bash_find instead
    when you need file NAMES/paths rather than contents, and bash_exec for
    anything else.
    HOW:
        - pattern (str): a regex, e.g. "def _execute", "TODO|FIXME",
          "error.*timeout".
        - path (str, default "."): file or directory to search; a file
          searches just that file, a directory searches recursively.
        - options (str, default "-n"): rg-style flags as a single string.
          Useful ones: "-n -i" (case-insensitive), "-n -t py" (Python
          files only), "-n -l" (just file names), "-n -A 3" (context
          lines), "-n -g '!test_*'" (exclude a glob), "-n --no-ignore
          --hidden" (search ignored/hidden files too).

    Like ripgrep, hidden files and .gitignore'd paths are skipped by
    default. Returns the matches, or "No matches." when nothing is found
    (this is not an error). Bad regexes and unknown options appear as an
    "[Error]" line instead of raising.
    """
    started = time.monotonic()
    root = resolve_existing(path)

    def run(pat: str, kwargs: dict):
        # rgapi rejects count and timeout_ms together.
        timeout_ms = None if kwargs.get("count") else DEFAULT_TIMEOUT * 1000
        return _engine().rg(pat, str(root), timeout_ms=timeout_ms, **kwargs)

    try:
        pattern, kwargs = _rg_kwargs(options, pattern)
        result = run(pattern, kwargs)
    except (ValueError, ImportError) as exc:
        return render(_search_error(exc), [], time.monotonic() - started)
    return render(_search_text(result, "No matches."), [], time.monotonic() - started)


def bash_ripgrep_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several ripgrep-style searches and return labeled results.

    WHAT: bash_ripgrep for many patterns at once — searches run on Rust
    threads through rgapi and the whole batch is capped by one timeout.
    WHEN: Use when you have several independent searches (e.g. scanning a
    codebase for multiple symbols before editing); use it instead of
    repeated bash_ripgrep calls to save round trips. For a single search,
    prefer bash_ripgrep.
    HOW:
        - searches (list[dict]): one dict per search, each with:
            * pattern (str, required): the regex to search for.
            * path (str, default "."): file or directory to search.
            * options (str, default "-n"): rg-style flags, as in
              bash_ripgrep.

    Results are labeled "pattern in path" sections. A search with no hits
    shows "No matches."; a bad spec or missing path shows an "Error:"
    section instead of failing the whole batch.
    """
    started = time.monotonic()
    deadline = time.monotonic() + DEFAULT_TIMEOUT
    sections: list[str] = []
    for spec in searches:
        label = f"{spec.get('pattern', '')} in {spec.get('path', '.')}"
        try:
            root = str(resolve_existing(spec.get("path", ".")))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            body = _run_search(
                spec,
                "-n",
                _rg_kwargs,
                lambda pat, kwargs, root=root, remaining_ms=remaining_ms: _engine().rg(
                    pat,
                    root,
                    timeout_ms=None if kwargs.get("count") else remaining_ms,
                    **kwargs,
                ),
            )
        except (KeyError, ValueError, OSError) as exc:
            body = f"Error: {exc}"
        sections.append(f"== {label} ==\n{body}")

    elapsed = time.monotonic() - started
    return "\n\n".join(sections) + f"\n({elapsed:.1f}s)"


def bash_find(pattern: str | None = None, path: str = ".", options: str = "") -> str:
    """
    Find file and directory paths by name.

    WHAT: Finds paths under `path` with the rgapi library (the fd/fdfind
    walking engine as a Python module, no fd binary needed) and returns
    them one per line. WHEN: Use to locate files/directories by name or
    extension — "where is the config?", "list all .py files", "find test
    dirs". Use bash_ripgrep instead when matching file CONTENTS, and
    bash_ls to see just one directory level.
    HOW:
        - pattern (str, optional): regex matched against file names. Omit
          or pass "" to list everything under `path`.
        - path (str, default "."): root directory of the search.
        - options (str, default ""): fd-style flags as a single string.
          Useful ones: "-e py" (extension), "-t f" (files only), "-t d"
          (directories only), "-d 2" (max depth), "-E node_modules"
          (exclude a glob), "--hidden" and "--no-ignore" (search
          ignored/hidden paths too).

    Like fd, hidden files and .gitignore'd paths are skipped by default.
    A pattern that matches nothing returns empty output (not an error).
    """
    started = time.monotonic()
    root = resolve_existing(path)

    def run(pat: str | None, kwargs: dict):
        return _engine().fd(
            pattern=pat, root=root, timeout_ms=DEFAULT_TIMEOUT * 1000, **kwargs
        )

    try:
        pattern, kwargs = _fd_kwargs(options, pattern or "")
        result = run(pattern, kwargs)
    except (ValueError, ImportError) as exc:
        return render(_search_error(exc), [], time.monotonic() - started)
    return render(_search_text(result, no_match=None), [], time.monotonic() - started)


def bash_find_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several path lookups and return labeled results.

    WHAT: bash_find for many patterns at once — all lookups run through
    rgapi under one shared timeout. WHEN: Use when you need several
    independent file listings (e.g. locating every module touched by a
    plan before editing); prefer bash_find for a single lookup.
    HOW:
        - searches (list[dict]): one dict per lookup, each with:
            * pattern (str, default ""): regex matched against file names.
            * path (str, default "."): root directory of the search.
            * options (str, default ""): fd-style flags, as in bash_find.

    Results are labeled "pattern in path" sections; a bad spec or missing
    path shows an "Error:" section instead of failing the whole batch.
    """
    started = time.monotonic()
    deadline = time.monotonic() + DEFAULT_TIMEOUT
    sections: list[str] = []
    for spec in searches:
        label = f"{spec.get('pattern', '')} in {spec.get('path', '.')}"
        try:
            root = str(resolve_existing(spec.get("path", ".")))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            body = _run_search(
                spec,
                "",
                _fd_kwargs,
                lambda pat, kwargs, root=root, remaining_ms=remaining_ms: _engine().fd(
                    pattern=pat or None, root=root, timeout_ms=remaining_ms, **kwargs
                ),
                require_pattern=False,
                no_match=None,
            )
        except (KeyError, ValueError, OSError) as exc:
            body = f"Error: {exc}"
        sections.append(f"== {label} ==\n{body}")

    elapsed = time.monotonic() - started
    return "\n\n".join(sections) + f"\n({elapsed:.1f}s)"


def bash_glob(pattern: str, path: str = ".", options: str = "") -> str:
    """
    Find file and directory paths matching a glob pattern.

    WHAT: Matches paths under `path` against a glob like "*.py",
    "src/**/*.py", or "tests/*_test.go" (via the rgapi library — no fd or
    glob binary needed) and returns them one per line, sorted. WHEN: Use
    when you think in globs rather than regexes — "all config files",
    "every module under src/". Use bash_find instead when you want a
    regex against file names, bash_ripgrep for file CONTENTS, and bash_ls
    to see one directory level.
    HOW:
        - pattern (str): a glob. Without a "/", it matches any path
          component at any depth ("*.py" finds every Python file); with a
          "/", it is anchored to `path` ("src/*.py" matches only
          directly inside src/). "*" does not cross directory separators.
        - path (str, default "."): root directory of the search.
        - options (str, default ""): fd-style flags as a single string.
          Useful ones: "-t f" (files only), "-t d" (directories only),
          "-d 2" (max depth), "-E node_modules" (exclude a glob),
          "--hidden" and "--no-ignore" (search ignored/hidden paths
          too).

    Like fd, hidden files and .gitignore'd paths are skipped by default.
    A pattern that matches nothing returns empty output (not an error).
    """
    started = time.monotonic()
    root = resolve_existing(path)

    def run(pat: str | None, kwargs: dict):
        result = _engine().fd(
            pattern=None, root=root, timeout_ms=DEFAULT_TIMEOUT * 1000, **kwargs
        )
        return sorted(result)

    try:
        _pattern, kwargs = _fd_kwargs(options, pattern, glob=True)
        result = run(pattern, kwargs)
    except (ValueError, ImportError) as exc:
        return render(_search_error(exc), [], time.monotonic() - started)
    return render(_search_text(result, no_match=None), [], time.monotonic() - started)


def bash_glob_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several glob lookups and return labeled results.

    WHAT: bash_glob for many patterns at once — all lookups run through
    rgapi under one shared timeout. WHEN: Use when you need several
    independent file listings (e.g. locating every config, test, and doc
    file before a change); prefer bash_glob for a single lookup.
    HOW:
        - searches (list[dict]): one dict per lookup, each with:
            * pattern (str, default ""): the glob to match.
            * path (str, default "."): root directory of the search.
            * options (str, default ""): fd-style flags, as in bash_glob.

    Results are labeled "pattern in path" sections, sorted per section;
    a bad spec or missing path shows an "Error:" section instead of
    failing the whole batch.
    """
    started = time.monotonic()
    deadline = time.monotonic() + DEFAULT_TIMEOUT
    sections: list[str] = []
    for spec in searches:
        label = f"{spec.get('pattern', '')} in {spec.get('path', '.')}"
        try:
            root = str(resolve_existing(spec.get("path", ".")))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            body = _run_search(
                spec,
                "",
                lambda opts, pat: _fd_kwargs(opts, pat, glob=True),
                lambda _pat, kwargs, root=root, remaining_ms=remaining_ms: sorted(
                    _engine().fd(
                        pattern=None, root=root, timeout_ms=remaining_ms, **kwargs
                    )
                ),
                require_pattern=False,
                no_match=None,
            )
        except (KeyError, ValueError, OSError) as exc:
            body = f"Error: {exc}"
        sections.append(f"== {label} ==\n{body}")

    elapsed = time.monotonic() - started
    return "\n\n".join(sections) + f"\n({elapsed:.1f}s)"
