"""Run one Agent Skill script inside an operating-system sandbox.

Skill scripts are ordinary programs bundled with a skill, but from the agent's
point of view they are untrusted code: the model chooses which one to run.
This module gives every run the same shape regardless of platform:

- the interpreter is resolved from the script suffix or its shebang and must
  be on an allowlist; nothing goes through a shell,
- the script must live inside the skill directory, and the guard is applied
  after ``resolve()`` so symlinks cannot escape it,
- the environment is replaced, not extended: no API keys or ambient secrets
  reach the script, and ``HOME``/``TMPDIR`` point at a throwaway directory,
- the process runs in its own group and the whole group is killed on timeout.

When an OS sandbox is available it wraps the command:

- ``bwrap`` (bubblewrap) on Linux: all namespaces unshared, the host mounted
  read-only, a tmpfs for ``/tmp``, and the network unshared unless allowed.
- ``sandbox-exec`` on macOS: write access is denied everywhere except the
  throwaway directory (plus the skill directory when writes are allowed),
  and the network is denied unless allowed.
- ``firejail`` on Linux is a fallback when bwrap is missing: read-only root,
  private ``/tmp``, ``--net=none`` unless allowed.

``sandbox="auto"`` prefers those backends and falls back to process
confinement (allowlist, path guard, scrubbed environment, timeout) when none
is installed. ``sandbox="required"`` refuses to fall back; ``sandbox="off"``
skips the OS layer entirely. Process confinement is not a security boundary --
a skill script can still touch anything the agent process can -- so use
``required`` when the scripts are not trusted.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .bash import _render

logger = logging.getLogger(__name__)

DEFAULT_SCRIPT_TIMEOUT = 30.0

SandboxName = Literal["auto", "required", "off", "bwrap", "firejail", "sandbox-exec"]
Backend = Literal["bwrap", "firejail", "sandbox-exec"]

DEFAULT_INTERPRETERS: tuple[str, ...] = (
    "python",
    "bash",
    "sh",
    "node",
    "deno",
    "ruby",
    "perl",
)

_SANDBOXES: tuple[SandboxName, ...] = (
    "auto",
    "required",
    "off",
    "bwrap",
    "firejail",
    "sandbox-exec",
)
_OS_BACKENDS: tuple[Backend, ...] = ("bwrap", "firejail", "sandbox-exec")

_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
    ".rb": "ruby",
    ".pl": "perl",
}


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """One script run: rendered output plus machine-readable facts."""

    output: str
    exit_code: int | None
    elapsed: float
    timed_out: bool
    truncated: bool
    backend: str


def _family(executable: str) -> str:
    """Normalize an interpreter path to its allowlist family name."""
    stem = Path(executable).name.casefold()
    stem = stem.removesuffix(".exe")
    if stem.startswith("python"):
        return "python"
    return stem


def _available(backend: Backend) -> bool:
    if backend == "sandbox-exec" and sys.platform != "darwin":
        return False
    return shutil.which(backend) is not None


def resolve_backend(request: SandboxName) -> Backend | None:
    """Resolve the requested sandbox to an installed backend.

    Returns ``None`` for process confinement. Explicit backends that are not
    installed (or not supported on this platform) raise instead of silently
    degrading.
    """
    if request not in _SANDBOXES:
        raise ValueError(
            f"Unknown sandbox {request!r}: expected one of {', '.join(_SANDBOXES)}"
        )
    if request == "off":
        return None
    if request in _OS_BACKENDS:
        if not _available(request):
            raise ValueError(
                f"Sandbox backend {request!r} is not available on this system"
            )
        return request
    if request == "required":
        for backend in _OS_BACKENDS:
            if _available(backend):
                return backend
        raise ValueError(
            "sandbox='required' but no OS sandbox is installed "
            "(install bubblewrap on Linux; macOS ships sandbox-exec)"
        )
    for backend in _OS_BACKENDS:
        if _available(backend):
            return backend
    logger.warning(
        "No OS sandbox available; skill scripts run with process confinement only"
    )
    return None


def _escape_profile_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace('"', '\\"')


def sandbox_exec_profile(
    skill_dir: Path, tmp_dir: Path, *, allow_network: bool, allow_write: bool
) -> str:
    """Build the macOS seatbelt profile for one run."""
    writable = [tmp_dir]
    if allow_write:
        writable.append(skill_dir)
    paths = " ".join(
        f'(subpath "{_escape_profile_path(str(path))}")'
        for path in (item.resolve() for item in writable)
    )
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        (
            "(allow file-write* "
            f'{paths} (literal "/dev/null") (literal "/dev/stdout") '
            '(literal "/dev/stderr") (literal "/dev/tty") (subpath "/dev/fd"))'
        ),
    ]
    if not allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def bwrap_argv(
    tail: Sequence[str],
    skill_dir: Path,
    *,
    allow_network: bool,
    allow_write: bool,
) -> list[str]:
    """Wrap ``tail`` with bubblewrap mounts and namespaces."""
    argv = ["bwrap", "--unshare-all"]
    if allow_network:
        argv.append("--share-net")
    argv += [
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]
    if allow_write:
        argv += ["--bind", str(skill_dir), str(skill_dir)]
    argv += ["--chdir", str(skill_dir), "--"]
    return [*argv, *tail]


def firejail_argv(
    tail: Sequence[str],
    skill_dir: Path,
    *,
    allow_network: bool,
    allow_write: bool,
) -> list[str]:
    """Wrap ``tail`` with firejail restrictions."""
    argv = ["firejail", "--quiet", "--noprofile", "--private-tmp"]
    if not allow_network:
        argv.append("--net=none")
    if not allow_write:
        argv.append("--read-only=/")
    argv.append(f"--working-directory={skill_dir}")
    return [*argv, "--", *tail]


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return text[:limit].rstrip() + f"\n[... {omitted} chars omitted ...]", True


class ScriptSandbox:
    """Executor for skill scripts with interpreter allowlisting and isolation."""

    def __init__(
        self,
        *,
        sandbox: SandboxName = "auto",
        timeout: float = DEFAULT_SCRIPT_TIMEOUT,
        allow_network: bool = False,
        allow_write: bool = False,
        interpreters: Sequence[str] = DEFAULT_INTERPRETERS,
        env: Mapping[str, str] | None = None,
        max_output_chars: int = 0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if max_output_chars < 0:
            raise ValueError("max_output_chars must be >= 0")
        allowed = tuple(str(name) for name in interpreters)
        if not allowed:
            raise ValueError("interpreters must contain at least one entry")
        self.backend = resolve_backend(sandbox)
        self.sandbox = sandbox
        self.timeout = float(timeout)
        self.allow_network = allow_network
        self.allow_write = allow_write
        self.interpreters = allowed
        self.env = dict(env or {})
        self.max_output_chars = max_output_chars

    @property
    def backend_name(self) -> str:
        return self.backend or "process"

    def _interpreter(self, script: Path) -> str:
        """Resolve the interpreter path, enforcing the allowlist."""
        family = _EXTENSIONS.get(script.suffix.casefold())
        executable: str | None = None

        if family is None:
            family, executable = self._shebang(script)
        if family is None:
            raise ValueError(
                f"Cannot determine an interpreter for {script.name!r}; "
                f"supported extensions: {', '.join(sorted(_EXTENSIONS))}"
            )

        if family not in self.interpreters:
            raise PermissionError(
                f"Interpreter {family!r} is not allowed; allowed: "
                f"{', '.join(self.interpreters)}"
            )

        if executable is None:
            executable = sys.executable if family == "python" else shutil.which(family)
        if not executable:
            raise FileNotFoundError(f"Interpreter not found on PATH: {family}")
        return executable

    @staticmethod
    def _shebang(script: Path) -> tuple[str | None, str | None]:
        """Read the first line and return ``(family, executable)``."""
        try:
            first = script.read_text(errors="replace").splitlines()[0]
        except (OSError, IndexError):
            return None, None
        if not first.startswith("#!"):
            return None, None
        parts = first[2:].strip().split()
        if not parts:
            return None, None
        candidate = parts[0]
        if Path(candidate).name == "env":
            candidate = next(
                (item for item in parts[1:] if not item.startswith("-")), ""
            )
        if not candidate:
            return None, None
        family = _family(candidate)
        return family, shutil.which(candidate)

    def _environment(self, skill_dir: Path, tmp: str) -> dict[str, str]:
        """Build a replacement environment for the script process."""
        lang = os.environ.get("LANG", "C.UTF-8")
        env = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            ),
            "HOME": tmp,
            "TMPDIR": tmp,
            "TMP": tmp,
            "LANG": lang,
            "LC_ALL": os.environ.get("LC_ALL", lang),
            "NO_COLOR": "1",
            "TERM": "dumb",
            "PWD": str(skill_dir),
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(self.env)
        return env

    def _wrap(
        self, tail: Sequence[str], skill_dir: Path, tmp_dir: Path | None
    ) -> list[str]:
        if self.backend == "bwrap":
            return bwrap_argv(
                tail,
                skill_dir,
                allow_network=self.allow_network,
                allow_write=self.allow_write,
            )
        if self.backend == "firejail":
            return firejail_argv(
                tail,
                skill_dir,
                allow_network=self.allow_network,
                allow_write=self.allow_write,
            )
        if self.backend == "sandbox-exec":
            assert tmp_dir is not None  # sandbox-exec writes to the host tmp dir
            profile = sandbox_exec_profile(
                skill_dir,
                tmp_dir,
                allow_network=self.allow_network,
                allow_write=self.allow_write,
            )
            return ["sandbox-exec", "-p", profile, *tail]
        return list(tail)

    def run(
        self, script: Path, args: Sequence[str] = (), *, skill_dir: Path
    ) -> ScriptResult:
        """Run ``script`` confined to ``skill_dir``.

        Raises ``ValueError`` when the script resolves outside the skill
        directory; everything else (bad exit, timeout, sandbox refusal) is
        reported in the result so the agent can read it.
        """
        skill_dir = skill_dir.resolve()
        script = script.resolve()
        try:
            script.relative_to(skill_dir)
        except ValueError:
            raise ValueError(
                f"Script {script} is outside the skill directory"
            ) from None
        if not script.is_file():
            raise FileNotFoundError(f"Script not found: {script}")

        executable = self._interpreter(script)
        tail = [executable, str(script), *(str(arg) for arg in args)]

        host_tmp = (
            Path(tempfile.mkdtemp(prefix="dspy-skill-"))
            if self.backend in (None, "sandbox-exec")
            else None
        )
        try:
            inside_tmp = str(host_tmp) if host_tmp is not None else "/tmp"
            env = self._environment(skill_dir, inside_tmp)
            argv = self._wrap(tail, skill_dir, host_tmp)
            output, exit_code, elapsed, timed_out = self._execute(argv, skill_dir, env)
        finally:
            if host_tmp is not None:
                shutil.rmtree(host_tmp, ignore_errors=True)

        rendered, truncated = _truncate(output, self.max_output_chars)
        notices = []
        if timed_out:
            notices.append(f"[script timed out after {self.timeout:g}s and was killed]")
        elif exit_code not in (0, None):
            notices.append(f"[exit code {exit_code}]")
        return ScriptResult(
            output=_render(rendered, notices, elapsed),
            exit_code=exit_code,
            elapsed=elapsed,
            timed_out=timed_out,
            truncated=truncated,
            backend=self.backend_name,
        )

    def _execute(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]
    ) -> tuple[str, int | None, float, bool]:
        """Run argv in its own process group with a kill-on-timeout deadline."""
        started = time.monotonic()
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw, _ = process.communicate(timeout=self.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:  # already gone between timeout and kill
                pass
            raw, _ = process.communicate()
            timed_out = True
        elapsed = time.monotonic() - started
        return raw.decode(errors="replace"), process.returncode, elapsed, timed_out


__all__ = [
    "DEFAULT_INTERPRETERS",
    "DEFAULT_SCRIPT_TIMEOUT",
    "SandboxName",
    "ScriptResult",
    "ScriptSandbox",
    "bwrap_argv",
    "firejail_argv",
    "resolve_backend",
    "sandbox_exec_profile",
]
