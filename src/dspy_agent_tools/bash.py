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


def run_command(
    command: str, cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT
) -> str:
    """
    Run a shell command and return its combined output as a string.

    stdout and stderr are merged, as in a terminal. When `cwd` is omitted, a
    leading "cd <path> && ..." on the command is used as the working directory
    and stripped from the command.

    `timeout` is a deadline in seconds; 0 disables it for long-running
    commands. On timeout the command's whole process group is killed and the
    output captured so far is returned with a notice.

    Notices are appended as trailing bracketed lines after the output:
    "[exit code N]" for a non-zero exit, "[command timed out after Ns...]",
    and "[output truncated: ...]" when the output exceeded the inline cap.
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


def ripgrep(pattern: str, path: str = ".", options: str = "-n") -> str:
    """
    Search file contents with ripgrep and return the matching lines.

    `options` are extra rg flags (e.g. "-n -i -t py"). No matches (rg exit
    code 1) is reported as "No matches." rather than an error.
    """
    output, notices, elapsed, rc = _execute(
        ["rg", *options.split(), pattern, str(_resolve_existing(path))],
        ok_codes=frozenset({1}),
    )
    if rc == 1 and not output.strip():
        output = "No matches.\n"
    return _render(output, notices, elapsed)


def ripgrep_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several ripgrep searches concurrently and return labeled results.

    Each dict needs "pattern" and optionally "path" and "options" (rg flags).
    """

    def build(spec: dict[str, str]) -> list[str]:
        return [
            "rg",
            *spec.get("options", "-n").split(),
            spec["pattern"],
            str(_resolve_existing(spec.get("path", "."))),
        ]

    return _run_batched(searches, build, ok_codes=frozenset({1}), no_match_rc=1)


def find(pattern: str | None = None, path: str = ".", options: str = "") -> str:
    """
    Find file paths under a directory using fd.

    `pattern` is an fd pattern (regex by default; pass --glob in `options`
    for glob matching). With no pattern, everything under `path` is listed.
    """
    return _render(
        *_execute(
            ["fd", *options.split(), pattern or "", str(_resolve_existing(path))]
        )[:3]
    )


def find_batch(searches: list[dict[str, str]]) -> str:
    """
    Run several fd lookups concurrently and return labeled results.

    Each dict needs "pattern" and optionally "path" and "options" (fd flags).
    """

    def build(spec: dict[str, str]) -> list[str]:
        return [
            "fd",
            *spec.get("options", "").split(),
            spec.get("pattern", ""),
            str(_resolve_existing(spec.get("path", "."))),
        ]

    return _run_batched(searches, build)


def curl(url: str, options: str = "-sSL") -> str:
    """
    Fetch a URL with curl and return the response body.

    `options` are curl flags; the default "-sSL" is silent, shows errors on
    stderr (merged into the output), and follows redirects.
    """
    output, notices, elapsed, _ = _execute(["curl", *options.split(), url])
    return _render(output, notices, elapsed)


def ls(path: str = ".", options: str = "-la") -> str:
    """List directory entries with ls.

    `options` are ls flags; the default "-la" shows all entries in long
    format.
    """
    output, notices, elapsed, _ = _execute(
        ["ls", *options.split(), str(_resolve_existing(path))]
    )
    return _render(output, notices, elapsed)
