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


def _resolve_cwd(cwd: str | None) -> Path:
    """Resolve and validate the working directory."""
    resolved = Path(cwd).expanduser() if cwd is not None else Path.cwd()
    resolved = resolved.resolve()
    if not resolved.exists():
        raise ValueError(f"Working directory does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Working directory is not a directory: {resolved}")
    return resolved


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

    cwd_path = _resolve_cwd(cwd)

    started = time.monotonic()
    proc = local["bash"]["-c", command].popen(
        cwd=str(cwd_path),
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
    elif popen.returncode != 0:
        notices.append(f"[exit code {popen.returncode}]")
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        output = (
            output[:_HEAD_BYTES] + "\n[... output truncated: showing first "
            f"{_HEAD_BYTES} and last {_TAIL_BYTES} of "
            f"{len(output.encode())} bytes ...]\n" + output[-_TAIL_BYTES:]
        )
        notices.append(
            f"[output truncated to first {_HEAD_BYTES} and last {_TAIL_BYTES} bytes]"
        )

    if notices:
        output = output.rstrip("\n") + "\n\n" + "\n".join(notices)

    return f"{output.rstrip()}\n({elapsed:.1f}s)"
