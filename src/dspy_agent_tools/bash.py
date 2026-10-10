import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

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


def _run_batched(
    specs: list[dict[str, str]],
    build_argv,
    ok_codes: frozenset[int] = frozenset(),
    no_match_rc: int | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run one subprocess per spec concurrently; return labeled outputs.

    Output is collected sequentially against one overall deadline, so the
    batch never outlives the timeout even if a single search stalls.
    """
    started = time.monotonic()

    running: list[tuple[str, subprocess.Popen[bytes]]] = []
    sections: list[str] = []
    for spec in specs:
        label = f"{spec.get('pattern', '')} in {spec.get('path', '.')}"
        try:
            argv = build_argv(spec)
            running.append((label, _spawn(argv)))
        except (KeyError, ValueError, OSError) as exc:
            sections.append(f"== {label} ==\nError: {exc}")

    deadline = time.monotonic() + timeout
    for label, proc in running:
        notices: list[str] = []
        try:
            raw, _ = proc.communicate(
                timeout=deadline - time.monotonic() if timeout > 0 else None
            )
        except subprocess.TimeoutExpired:
            pid = proc.pid
            assert pid is not None  # a process that timed out still has a pid
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            raw, _ = proc.communicate()
            notices.append(f"[timed out after {timeout}s and was killed]")

        output = raw.decode(errors="replace")
        rc = proc.returncode
        if rc == no_match_rc and not output.strip():
            output = "No matches.\n"
        elif rc != 0 and rc not in ok_codes:
            notices.append(f"[exit code {rc}]")
        output, cap_notice = _cap(output)
        if cap_notice is not None:
            notices.append(cap_notice)
        section = f"== {label} ==\n" + output.rstrip("\n")
        if notices:
            section += "\n\n" + "\n".join(notices)
        sections.append(section)

    elapsed = time.monotonic() - started
    return "\n\n".join(sections) + f"\n({elapsed:.1f}s)"


def _spawn(argv: list[str]):
    """Start a process with merged output in its own process group."""
    return local[argv[0]][tuple(argv[1:])].popen(
        stderr=subprocess.STDOUT, new_session=True
    )


def bash_ripgrep(pattern: str, path: str = ".", options: str = "-n") -> str:
    """
    Search file contents with ripgrep and return the matching lines.

    WHAT: Runs `rg` and returns lines matching a regex, prefixed with
    "file:line:". WHEN: The default tool for finding code, text, symbols,
    or config inside files. Use bash_find instead when you need file
    NAMES/paths rather than contents, and bash_exec for anything else.
    HOW:
        - pattern (str): a regex, e.g. "def _execute", "TODO|FIXME",
          "error.*timeout".
        - path (str, default "."): file or directory to search; a file
          searches just that file, a directory searches recursively.
        - options (str, default "-n"): extra rg flags as a single string.
          Useful ones: "-n -i" (case-insensitive), "-n -t py" (Python
          files only), "-n -l" (just file names), "-n -A 3" (context
          lines), "-n --no-ignore" (search ignored/hidden files too).

    Returns the matches, or "No matches." when nothing is found (this is
    not an error). Non-rg errors (bad regex, missing path) appear as an
    "[exit code 2]" notice with rg's message.
    """
    output, notices, elapsed, rc = _execute(
        ["rg", *options.split(), pattern, str(_resolve_existing(path))],
        ok_codes=frozenset({1}),
    )
    if rc == 1 and not output.strip():
        output = "No matches.\n"
    return _render(output, notices, elapsed)


def bash_ripgrep_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several ripgrep searches concurrently and return labeled results.

    WHAT: bash_ripgrep for many patterns at once — all searches run as
    concurrent processes and the whole batch is capped by one timeout.
    WHEN: Use when you have several independent searches (e.g. scanning a
    codebase for multiple symbols before editing); use it instead of
    repeated bash_ripgrep calls to save round trips. For a single search,
    prefer bash_ripgrep.
    HOW:
        - searches (list[dict]): one dict per search, each with:
            * pattern (str, required): the regex to search for.
            * path (str, default "."): file or directory to search.
            * options (str, default "-n"): extra rg flags.

    Results are labeled "pattern in path" sections. A search with no hits
    shows "No matches."; a bad spec or missing path shows an "Error:"
    section instead of failing the whole batch.
    """

    def build(spec: dict[str, str]) -> list[str]:
        return [
            "rg",
            *spec.get("options", "-n").split(),
            spec["pattern"],
            str(_resolve_existing(spec.get("path", "."))),
        ]

    return _run_batched(searches, build, ok_codes=frozenset({1}), no_match_rc=1)


def bash_find(pattern: str | None = None, path: str = ".", options: str = "") -> str:
    """
    Find file and directory paths by name using fd.

    WHAT: Runs `fd` and returns matching paths under `path`, one per line.
    WHEN: Use to locate files/directories by name or extension — "where is
    the config?", "list all .py files", "find test dirs". Use
    bash_ripgrep instead when matching file CONTENTS, and bash_ls to see
    just one directory level.
    HOW:
        - pattern (str, optional): fd pattern, a regex by default. Omit or
          pass "" to list everything under `path`.
        - path (str, default "."): root directory of the search.
        - options (str, default ""): extra fd flags as a single string.
          Useful ones: "--glob" (treat pattern as a glob like "*.py"),
          "--type f" (files only), "--type d" (directories only),
          "--hidden", "--max-depth 2", "--exclude node_modules".

    fd ignores hidden files and .gitignore'd paths by default; add
    "--hidden" and/or "--no-ignore" in options to see them.
    """
    output, notices, elapsed, _ = _execute(
        ["fd", *options.split(), pattern or "", str(_resolve_existing(path))]
    )
    return _render(output, notices, elapsed)


def bash_find_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several fd path lookups concurrently and return labeled results.

    WHAT: bash_find for many patterns at once — all lookups run as
    concurrent processes under one shared timeout. WHEN: Use when you need
    several independent file listings (e.g. locating every module touched
    by a plan before editing); prefer bash_find for a single lookup.
    HOW:
        - searches (list[dict]): one dict per lookup, each with:
            * pattern (str, default ""): fd pattern (regex, or glob with
              the "--glob" option).
            * path (str, default "."): root directory of the search.
            * options (str, default ""): extra fd flags.

    Results are labeled "pattern in path" sections; a bad spec or missing
    path shows an "Error:" section instead of failing the whole batch.
    """

    def build(spec: dict[str, str]) -> list[str]:
        return [
            "fd",
            *spec.get("options", "").split(),
            spec.get("pattern", ""),
            str(_resolve_existing(spec.get("path", "."))),
        ]

    return _run_batched(searches, build)


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
