import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

import rgapi
from plumbum import local

DEFAULT_TIMEOUT = 300  # seconds
MAX_OUTPUT_BYTES = 30_000  # inline cap; head+tail are kept above it
_HEAD_BYTES = 20_000
_TAIL_BYTES = 10_000

# Leading "cd <path> &&" on the first line, single plain token only. Anything
# else (redirects, quoted paths with spaces, more args) is left to the shell.
_CD_PATTERN = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*")


def _resolve_existing(path: str | None) -> Path:
    """Expand, resolve, and verify a path exists (file or directory)."""
    resolved = Path(path).expanduser().resolve() if path is not None else Path.cwd()
    if not resolved.exists():
        raise ValueError(f"Path does not exist: {resolved}")
    return resolved


def _resolve_cwd(cwd: str | None) -> Path:
    """Resolve and validate the working directory."""
    resolved = _resolve_existing(cwd)
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return resolved


def _cap(output: str) -> tuple[str, str | None]:
    """Clamp output to the inline cap, keeping head and tail."""
    encoded = output.encode()
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return output, None
    return (
        encoded[:_HEAD_BYTES].decode(errors="replace")
        + f"\n[... output truncated: showing first {_HEAD_BYTES} and last "
        f"{_TAIL_BYTES} of {len(encoded)} bytes ...]\n"
        + encoded[-_TAIL_BYTES:].decode(errors="replace"),
        f"[output truncated to first {_HEAD_BYTES} and last {_TAIL_BYTES} bytes]",
    )


def _execute(
    argv: list[str],
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    ok_codes: frozenset[int] = frozenset(),
) -> tuple[str, list[str], float, int | None]:
    """Run argv with merged output, a kill-on-timeout deadline, and the inline cap.

    Returns (output, notices, elapsed_seconds, exit_code).
    """
    started = time.monotonic()
    proc = local[argv[0]][tuple(argv[1:])].popen(
        cwd=None if cwd is None else str(cwd),
        stderr=subprocess.STDOUT,
        new_session=True,  # own process group so timeout kills children too
    )
    # PlumbumLocalPopen forwards attribute access to the wrapped Popen.
    popen = cast(subprocess.Popen[bytes], proc)
    try:
        raw, _ = popen.communicate(timeout=timeout if timeout > 0 else None)
        timed_out = False
    except subprocess.TimeoutExpired:
        pid = popen.pid
        assert pid is not None  # a process that timed out still has a pid
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        raw, _ = popen.communicate()
        timed_out = True
    elapsed = time.monotonic() - started

    output = raw.decode(errors="replace")

    notices: list[str] = []
    if timed_out:
        notices.append(f"[command timed out after {timeout}s and was killed]")
    elif popen.returncode != 0 and popen.returncode not in ok_codes:
        notices.append(f"[exit code {popen.returncode}]")
    output, cap_notice = _cap(output)
    if cap_notice is not None:
        notices.append(cap_notice)
    return output, notices, elapsed, popen.returncode


def _render(output: str, notices: list[str], elapsed: float) -> str:
    """Append notices as trailing bracketed lines and the wall time."""
    if notices:
        output = output.rstrip("\n") + "\n\n" + "\n".join(notices)
    return f"{output.rstrip()}\n({elapsed:.1f}s)"


def bash_exec(
    command: str, cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT
) -> str:
    """
    Run an arbitrary shell command and return its combined output.

    WHAT: Executes `command` in bash and returns stdout and stderr merged,
    exactly as a terminal would show them, followed by a "(Ns)" wall time.
    WHEN: Use for anything without a dedicated tool — running builds or
    tests, git, python snippets, package installs. Prefer the dedicated
    tools when they fit: bash_ripgrep for searching text, bash_find for
    locating files, bash_ls for listing a directory, bash_curl for
    fetching URLs.
    HOW:
        - command (str): the shell line to run, e.g. "uv run pytest -q".
        - cwd (str, optional): working directory. Defaults to the current
          directory; a leading "cd <path> && ..." in the command is also
          accepted and used as the working directory.
        - timeout (int, default 300): deadline in seconds; 0 disables the
          deadline for long-running commands. On timeout the command's
          whole process group is killed and partial output is returned
          with a "[command timed out ...]" notice.

    Large output is truncated to its first and last ~10-20KB with a
    notice; non-zero exits are marked "[exit code N]" in the output, not
    raised. Pipe through "head", "grep", or redirect to a file when you
    expect huge output.
    """
    if timeout < 0:
        raise ValueError("timeout must be >= 0 (0 disables the deadline).")

    if cwd is None:
        match = _CD_PATTERN.match(command)
        if match is not None:
            cwd = match.group(1)
            command = command[match.end() :]

    output, notices, elapsed, _ = _execute(
        ["bash", "-c", command], cwd=_resolve_cwd(cwd), timeout=timeout
    )
    return _render(output, notices, elapsed)


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


def _fd_kwargs(options: str, pattern: str | None) -> tuple[str | None, dict]:
    """Translate fd flags into rgapi.fd keyword arguments.

    Returns (pattern, kwargs). With "--glob" the pattern is reinterpreted
    as an include glob; otherwise it stays a regex matched against file
    names. Raises ValueError for flags with no rgapi equivalent.
    """
    glob_mode = False
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


def _search_text(result, no_match: str | None) -> str:
    """Render an rgapi result with timeout/cap notices, without wall time."""
    notices: list[str] = []
    if getattr(result, "stop_reason", None) == "timeout":
        notices.append("[search timed out; partial results shown]")
    if isinstance(result, int):
        text = str(result)  # rg count=True returns a bare count
    else:
        text = "\n".join(str(item) for item in result)
    if no_match is not None and not text.strip():
        text = no_match
    text, cap_notice = _cap(text)
    if cap_notice is not None:
        notices.append(cap_notice)
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
    except ValueError as exc:  # bad regex, unreadable root, ...
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
    root = _resolve_existing(path)

    def run(pat: str, kwargs: dict):
        # rgapi rejects count and timeout_ms together.
        timeout_ms = None if kwargs.get("count") else DEFAULT_TIMEOUT * 1000
        return rgapi.rg(pat, str(root), timeout_ms=timeout_ms, **kwargs)

    try:
        pattern, kwargs = _rg_kwargs(options, pattern)
        result = run(pattern, kwargs)
    except ValueError as exc:
        return _render(_search_error(exc), [], time.monotonic() - started)
    return _render(_search_text(result, "No matches."), [], time.monotonic() - started)


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
            root = str(_resolve_existing(spec.get("path", ".")))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            body = _run_search(
                spec,
                "-n",
                _rg_kwargs,
                lambda pat, kwargs, root=root, remaining_ms=remaining_ms: rgapi.rg(
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
    root = _resolve_existing(path)

    def run(pat: str | None, kwargs: dict):
        return rgapi.fd(root, pat, timeout_ms=DEFAULT_TIMEOUT * 1000, **kwargs)

    try:
        pattern, kwargs = _fd_kwargs(options, pattern or "")
        result = run(pattern, kwargs)
    except ValueError as exc:
        return _render(_search_error(exc), [], time.monotonic() - started)
    return _render(_search_text(result, no_match=None), [], time.monotonic() - started)


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
            root = str(_resolve_existing(spec.get("path", ".")))
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            body = _run_search(
                spec,
                "",
                _fd_kwargs,
                lambda pat, kwargs, root=root, remaining_ms=remaining_ms: rgapi.fd(
                    root, pat or None, timeout_ms=remaining_ms, **kwargs
                ),
                require_pattern=False,
                no_match=None,
            )
        except (KeyError, ValueError, OSError) as exc:
            body = f"Error: {exc}"
        sections.append(f"== {label} ==\n{body}")

    elapsed = time.monotonic() - started
    return "\n\n".join(sections) + f"\n({elapsed:.1f}s)"


def bash_curl(url: str, options: str = "-sSL") -> str:
    """
    Fetch a URL with curl and return the response body.

    WHAT: Runs `curl` and returns the raw response body as a string.
    WHEN: Use for fetching web pages, APIs, or any http(s) content —
    including JSON APIs (pair with jq via bash_exec when needed). Not for
    searching local files; not for pages that need JavaScript rendering.
    HOW:
        - url (str): the URL to fetch, e.g. "https://api.github.com/...".
        - options (str, default "-sSL"): curl flags as a single string.
          The default is silent, shows errors on failure (stderr is merged
          into the output), and follows redirects. Useful additions:
          "-i" (include response headers), "-H 'Auth: Bearer ...'"
          (headers), "-X POST -d '{...}'" (requests), "-m 20" (per-request
          timeout), "-o /dev/null -w '%{http_code}'" (status only).

    A failed transfer (bad URL, connection refused) is reported as
    "[exit code N]" with curl's error message rather than an exception.

    For readable page text instead of raw HTML, use bash_fetch.
    """
    output, notices, elapsed, _ = _execute(["curl", *options.split(), url])
    return _render(output, notices, elapsed)


_JUNK_TAGS = ["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header"]
_JUNK_PATTERNS = (
    "ad",
    "ads",
    "advert",
    "banner",
    "cookie",
    "consent",
    "popup",
    "modal",
    "sidebar",
    "related",
    "recommendation",
    "social",
    "share",
    "comment",
    "disqus",
    "newsletter",
    "subscribe",
)


def bash_fetch(url: str, options: str = "-sSL") -> str:
    """
    Fetch a web page and return its readable content as markdown.

    WHAT: Downloads the URL (via curl, like bash_curl) and converts the
    HTML to clean markdown: scripts, styles, nav/header/footer, ads,
    cookie banners, sidebars, and similar chrome are stripped, the
    <article>/<main>/<body> content is kept, and the page <title> becomes
    a leading "# heading". WHEN: Use whenever you want to READ a web page
    (docs, articles, blog posts) instead of parsing raw HTML. Use
    bash_curl instead when you need the exact raw response (API JSON,
    headers, binary). Not suitable for pages that require JavaScript to
    render.
    HOW:
        - url (str): the page URL, e.g. "https://example.com/docs".
        - options (str, default "-sSL"): curl flags passed through,
          exactly as in bash_curl; you rarely need to change them.

    Returns markdown text; large results are truncated to the first and
    last ~10-20KB. A failed transfer is reported with a "[exit code N]"
    notice and curl's error instead of the markdown.
    """
    output, notices, elapsed, rc = _execute(["curl", *options.split(), url])
    if rc != 0:
        return _render(output, notices, elapsed)
    if not output.strip():
        return _render("[Error] Empty response from URL", notices, elapsed)

    try:
        from bs4 import BeautifulSoup, Comment
        from markdownify import markdownify as md
    except ImportError:
        return _render(
            "[Error] bs4 and markdownify are required for HTML conversion",
            notices,
            elapsed,
        )

    try:
        soup = BeautifulSoup(output, "html.parser")

        for tag in soup.find_all(_JUNK_TAGS):
            tag.decompose()

        for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
            comment.extract()

        to_remove = []
        for el in soup.find_all(True):
            classes = " ".join(el.get("class") or []).lower()
            el_id = str(el.get("id") or "").lower()
            if any(p in classes or p in el_id for p in _JUNK_PATTERNS):
                to_remove.append(el)
        for el in to_remove:
            el.decompose()

        content = soup.find("article") or soup.find("main") or soup.find("body") or soup
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        body_md = md(str(content), heading_style="ATX", strip=["img"])

        cleaned: list[str] = []
        blanks = 0
        for line in body_md.split("\n"):
            stripped = line.rstrip()
            if not stripped:
                blanks += 1
                if blanks <= 2:
                    cleaned.append("")
            else:
                blanks = 0
                cleaned.append(stripped)

        result = "\n".join(cleaned).strip()
        if title:
            result = f"# {title}\n\n{result}"
        return _render(result, notices, elapsed)
    except Exception as exc:  # noqa: BLE001 — conversion failure returns text, never raises
        return _render(f"[Error] HTML processing failed: {exc}", notices, elapsed)


def bash_ls(path: str = ".", options: str = "-la") -> str:
    """
    List the entries of a directory with ls.

    WHAT: Runs `ls` on one directory and returns its entries in long
    format. WHEN: Use to see what's directly inside a directory — file
    names, sizes, timestamps. Use bash_find to walk a whole tree by
    pattern, and bash_ripgrep to search file contents.
    HOW:
        - path (str, default "."): directory to list.
        - options (str, default "-la"): ls flags as a single string; the
          default shows all entries (including dotfiles) in long format.
          Useful ones: "-l" (long, no dotfiles), "-lah" (human sizes),
          "-lt" (newest first), "-1" (one per line).
    """
    output, notices, elapsed, _ = _execute(
        ["ls", *options.split(), str(_resolve_existing(path))]
    )
    return _render(output, notices, elapsed)
