"""Run an arbitrary shell command for the bash_exec tool."""

import re

from ..utils.process import DEFAULT_TIMEOUT, execute, render, resolve_cwd

# Leading "cd <path> &&" on the first line, single plain token only. Anything
# else (redirects, quoted paths with spaces, more args) is left to the shell.
_CD_PATTERN = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*")


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

    Output is returned in full; non-zero exits are marked "[exit code N]"
    in the output, not raised. Pipe through "head", "grep", or redirect to
    a file when you expect huge output.
    """
    if timeout < 0:
        raise ValueError("timeout must be >= 0 (0 disables the deadline).")

    if cwd is None:
        match = _CD_PATTERN.match(command)
        if match is not None:
            cwd = match.group(1)
            command = command[match.end() :]

    output, notices, elapsed, _ = execute(
        ["bash", "-c", command], cwd=resolve_cwd(cwd), timeout=timeout
    )
    return render(output, notices, elapsed)
