"""Run a child process and render its output.

Families that wrap a program — the shell tools and the CLI discovery tools —
share one runner so a timeout, an exit notice, and the trailing wall time look
the same everywhere.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import cast

from plumbum import local

DEFAULT_TIMEOUT = 300  # seconds


def resolve_existing(path: str | None) -> Path:
    """Expand, resolve, and verify a path exists (file or directory)."""
    resolved = Path(path).expanduser().resolve() if path is not None else Path.cwd()
    if not resolved.exists():
        raise ValueError(f"Path does not exist: {resolved}")
    return resolved


def resolve_cwd(cwd: str | None) -> Path:
    """Resolve and validate the working directory."""
    resolved = resolve_existing(cwd)
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return resolved


def execute(
    argv: list[str],
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    ok_codes: frozenset[int] = frozenset(),
    env: dict[str, str] | None = None,
) -> tuple[str, list[str], float, int | None]:
    """Run argv with merged output and a kill-on-timeout deadline.

    Returns the complete output (no truncation), notices, elapsed seconds,
    and the exit code. ``env`` adds or overrides variables for the child
    process only; ``None`` inherits the current environment.
    """
    started = time.monotonic()
    command = local[argv[0]]
    if env:
        command = command.with_env(**env)
    proc = command[tuple(argv[1:])].popen(
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
    return output, notices, elapsed, popen.returncode


def render(output: str, notices: list[str], elapsed: float) -> str:
    """Append notices as trailing bracketed lines and the wall time."""
    if notices:
        output = output.rstrip("\n") + "\n\n" + "\n".join(notices)
    return f"{output.rstrip()}\n({elapsed:.1f}s)"
