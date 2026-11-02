"""Expose an Agent Skills folder as ``dspy.Tool`` objects.

A skill is a directory with a ``SKILL.md`` file: YAML frontmatter carrying
``name`` and ``description``, followed by Markdown instructions. Skills may
bundle ``scripts/``, ``references/``, and ``assets/`` directories. The format
is the open [Agent Skills](https://agentskills.io/specification) standard, so
skills written for Claude Code, Codex, and other clients work unchanged.

``Skills`` works like :func:`dspy_agent_tools.CLI` and
:func:`dspy_agent_tools.MCP`: point it at the folders that house the skills and
it returns a :class:`SkillTools` list you can pass to any DSPy agent.

```python
import dspy
from dspy_agent_tools import Skills, bash_exec

skills = Skills("~/.agents/skills", ".agents/skills")

agent = dspy.ReAct("question -> answer", tools=skills + [bash_exec])
agent = dspy.RLM("question -> answer", tools=Skills("./skills", mode="code"))
```

The list holds four progressive-disclosure tools:

- ``list_skills`` — names, descriptions, and bundled scripts (~100 tokens per
  skill; the ``SKILL.md`` body is never loaded here).
- ``activate_skill`` — the full ``SKILL.md`` body for one skill.
- ``read_skill_resource`` — one file from ``references/`` or ``assets/``.
- ``run_skill_script`` — one script from ``scripts/``, run in the sandbox
  described in :mod:`dspy_agent_tools.sandbox`.

Call :meth:`SkillTools.patch` on an already-built agent to append the tools
and inject the skill catalog into its instructions; the agent class decides
how the inner predictor is rebuilt, so it works for ``dspy.ReAct``,
``dspy.ReActV2``, and ``dspy.RLM``.

``mode="search"`` (default) returns text for ReAct-style agents;
``mode="code"`` returns dictionaries for ``dspy.RLM``.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from .sandbox import (
    DEFAULT_INTERPRETERS,
    DEFAULT_SCRIPT_TIMEOUT,
    SandboxName,
    ScriptSandbox,
)

if TYPE_CHECKING:
    from dspy import Tool

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500

DEFAULT_MAX_CATALOG_CHARS = 8_000
DEFAULT_MAX_RESOURCE_CHARS = 50_000

_SKILL_FILE = "SKILL.md"
_MODES = ("search", "code")
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".mp3",
        ".mp4",
        ".wav",
    }
)

_GUIDANCE = """## Agent Skills

Skills are folders of instructions, scripts, and reference files. The catalog
below lists every installed skill; activate one before following it.

- `{list_skills}` — names, descriptions, bundled files, and any declared
  `allowed-tools`.
- `{activate_skill}` — load one skill's full SKILL.md instructions.
- `{read_skill_resource}` — read a file from a skill's references/ or assets/.
- `{run_skill_script}` — run a file from a skill's scripts/ in a sandbox."""

_GUIDANCE_NO_SCRIPTS = _GUIDANCE.rsplit("\n", 1)[0]

_HOST_RULES = (
    "A skill's instructions may also call for host commands (for example "
    "`npx skills find ...` or `gh skill search ...`): run them only with a "
    "shell or CLI tool you were given. If you were given none, name the exact "
    "command and tell the user to run it; never claim you ran a command and "
    "never invent its output, search results, install counts, or repository "
    "names."
)

_EXECUTION_NOTE = (
    "Run a skill through its tools; never simulate a result. `{run_skill_script}` "
    "starts one file from `scripts/` on the machine inside a sandbox, and "
    "nothing else bundled with a skill can be executed. " + _HOST_RULES
)

_EXECUTION_NOTE_NO_SCRIPTS = (
    "Run a skill through its tools; never simulate a result. Bundled scripts "
    "cannot be executed here: script running is disabled for this catalog, so "
    "a skill that needs a script can only be followed by hand. " + _HOST_RULES
)

_REPL_NOTE = (
    "Your code runs in a REPL that cannot spawn processes or open network "
    "connections, so shell commands fail from code. Tool calls, including "
    "`{run_skill_script}`, run on the machine instead."
)

_HOST_TOOLS_NOTE = "Host command tools available to you: {names}."

#: Tools from this package that execute host commands; used to tell the model
#: whether it can follow a skill's CLI instructions.
_HOST_TOOL_MODULES = frozenset({"dspy_agent_tools.bash", "dspy_agent_tools.cli"})
_SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "bash_exec",
        "exec",
        "execute",
        "run_shell",
        "shell",
        "sh",
        "terminal",
    }
)
_SHELL_TOOL_SUFFIXES = ("_run_command", "_bash_exec")

_LIST_DESC = (
    "List the installed agent skills with their descriptions and bundled "
    "scripts. Call this first when a task might match a skill, then call "
    "activate_skill before following its instructions."
)
_ACTIVATE_DESC = (
    "Load the full SKILL.md instructions for one skill, plus the scripts, "
    "references, and assets it bundles. Call it as soon as the task matches a "
    "skill description; then follow the returned instructions."
)
_READ_DESC = (
    "Read one reference or asset file bundled with a skill. Use the relative "
    "path shown by activate_skill, for example 'references/forms.md'. Paths "
    "outside the skill folder are rejected."
)
_RUN_DESC = (
    "Run one script bundled in a skill's scripts/ folder. The script runs "
    "with an allowlisted interpreter inside the skill sandbox; arguments are "
    "passed as an argv list, never through a shell. Run it with ['--help'] "
    "first when its usage is unclear."
)
_CODE_LIST_DESC = (
    "List the installed agent skills as a list of {name, description, "
    "allowed_tools, scripts, references, assets} dicts, sorted by name."
)
_CODE_ACTIVATE_DESC = (
    "Load one skill's full SKILL.md instructions as a dict with name, "
    "description, path, instructions, allowed_tools, scripts, references, "
    "assets, license, compatibility, and metadata."
)
_CODE_READ_DESC = (
    "Read one reference or asset file from a skill and return {path, "
    "content, truncated, error}."
)
_CODE_RUN_DESC = (
    "Run one script from a skill's scripts/ folder and return {script, "
    "exit_code, timed_out, elapsed, output, truncated, sandbox}."
)


def _load_tool() -> type[Tool]:
    """Import ``dspy.Tool`` on first use."""
    try:
        from dspy import Tool
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "dspy_agent_tools.Skills requires DSPy. Install it with: uv add dspy"
        ) from exc
    return Tool


# ---------------------------------------------------------------------------
# Skill model and discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skill:
    """One discovered skill: frontmatter plus its directory."""

    name: str
    description: str
    path: Path
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def skill_md(self) -> Path:
        """The ``SKILL.md`` file (or lower-case ``skill.md``) for this skill."""
        for name in (_SKILL_FILE, "skill.md"):
            candidate = self.path / name
            if candidate.is_file():
                return candidate
        return self.path / _SKILL_FILE

    def scripts(self) -> list[str]:
        """Relative paths of every file under ``scripts/``."""
        return _files(self.path / "scripts")

    def references(self) -> list[str]:
        """Relative paths of every file under ``references/``."""
        return _files(self.path / "references")

    def assets(self) -> list[str]:
        """Relative paths of every file under ``assets/``."""
        return _files(self.path / "assets")

    def instructions(self) -> str:
        """Read and return the Markdown body of ``SKILL.md``."""
        text = self.skill_md.read_text(errors="replace")
        _, body = _split_frontmatter(text)
        return body.strip()

    def resource(self, relative: str) -> Path:
        """Resolve ``relative`` inside this skill, rejecting escapes."""
        if not relative or not relative.strip():
            raise ValueError("A resource path is required")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ValueError(f"Resource path must be relative: {relative}")
        base = self.path.resolve()
        target = (base / candidate).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(
                f"Resource path escapes the skill directory: {relative}"
            ) from None
        if not target.is_file():
            raise FileNotFoundError(f"Resource not found: {relative}")
        return target


def _files(base: Path) -> list[str]:
    """Sorted relative file paths below ``base``, skipping hidden files."""
    if not base.is_dir():
        return []
    root = base.resolve()
    found = []
    for item in base.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(base)
        if any(
            part.startswith(".") or part == "__pycache__" for part in relative.parts
        ):
            continue
        try:
            item.resolve().relative_to(root)
        except ValueError:  # symlink pointing outside the folder
            continue
        found.append(relative.as_posix())
    return sorted(found)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``---`` frontmatter from the Markdown body."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter (---)")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    raise ValueError("SKILL.md frontmatter is not closed with ---")


def _frontmatter(text: str) -> dict[str, Any]:
    front, _ = _split_frontmatter(text)
    try:
        parsed = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TypeError("SKILL.md frontmatter must be a YAML mapping")
    return parsed


def _validate_name(name: str, skill_dir: Path) -> list[str]:
    problems: list[str] = []
    if not name:
        return ["Field 'name' must be a non-empty string"]
    if len(name) > MAX_NAME_CHARS:
        problems.append(f"name exceeds {MAX_NAME_CHARS} characters")
    if name != name.lower():
        problems.append("name must be lowercase")
    if name.startswith("-") or name.endswith("-"):
        problems.append("name must not start or end with a hyphen")
    if "--" in name:
        problems.append("name must not contain consecutive hyphens")
    if not all(char.isalnum() or char == "-" for char in name):
        problems.append("name may only contain letters, digits, and hyphens")
    if skill_dir.name != name:
        problems.append(
            f"directory name {skill_dir.name!r} must match skill name {name!r}"
        )
    return problems


def _validate(info: Skill) -> list[str]:
    problems: list[str] = []
    if not info.description.strip():
        problems.append("Field 'description' must be non-empty")
    elif len(info.description) > MAX_DESCRIPTION_CHARS:
        problems.append(f"description exceeds {MAX_DESCRIPTION_CHARS} characters")
    if (
        info.compatibility is not None
        and len(info.compatibility) > MAX_COMPATIBILITY_CHARS
    ):
        problems.append(f"compatibility exceeds {MAX_COMPATIBILITY_CHARS} characters")
    return problems


def _load_skill(skill_dir: Path) -> Skill:
    """Parse one skill directory into a :class:`Skill`."""
    skill_md = next(
        (
            skill_dir / name
            for name in (_SKILL_FILE, "skill.md")
            if (skill_dir / name).is_file()
        ),
        None,
    )
    if skill_md is None:
        raise ValueError(f"{_SKILL_FILE} not found in {skill_dir}")
    meta = _frontmatter(skill_md.read_text(errors="replace"))

    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Field 'name' must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Field 'description' must be a non-empty string")

    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise TypeError("Field 'metadata' must be a mapping")
    allowed_tools = meta.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        raise TypeError("Field 'allowed-tools' must be a string")
    for optional in ("license", "compatibility"):
        value = meta.get(optional)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"Field {optional!r} must be a string")

    return Skill(
        name=name.strip(),
        description=description.strip(),
        path=skill_dir,
        license=meta.get("license"),
        compatibility=meta.get("compatibility"),
        allowed_tools=allowed_tools,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


def _skill_dirs(root: Path) -> list[Path]:
    """Return the candidate skill directories under one root.

    A root that is itself a skill (has ``SKILL.md``) is used directly;
    otherwise its immediate subdirectories are the candidates.
    """
    if any((root / name).is_file() for name in (_SKILL_FILE, "skill.md")):
        return [root]
    return sorted(child for child in root.iterdir() if child.is_dir())


def _discover(roots: Sequence[Path], *, strict: bool) -> tuple[list[Skill], list[str]]:
    skills: list[Skill] = []
    errors: list[str] = []
    seen: dict[str, Path] = {}

    for root in roots:
        root = root.expanduser()
        if not root.is_dir():
            errors.append(f"{root}: not a directory")
            continue
        try:
            candidates = _skill_dirs(root)
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            continue
        for skill_dir in candidates:
            if not any(
                (skill_dir / name).is_file() for name in (_SKILL_FILE, "skill.md")
            ):
                continue
            try:
                info = _load_skill(skill_dir)
            except (OSError, TypeError, ValueError) as exc:
                errors.append(f"{skill_dir}: {exc}")
                continue
            problems = _validate_name(info.name, skill_dir) + _validate(info)
            if problems:
                errors.append(f"{skill_dir}: {'; '.join(problems)}")
                continue
            if info.name in seen:
                errors.append(
                    f"{skill_dir}: duplicate skill name {info.name!r} "
                    f"(already loaded from {seen[info.name]})"
                )
                continue
            seen[info.name] = skill_dir
            skills.append(info)

    if strict and errors:
        raise ValueError("Invalid skills:\n" + "\n".join(errors))
    for error in errors:
        logger.warning("Skipping skill: %s", error)
    return skills, errors


# ---------------------------------------------------------------------------
# Catalog rendering
# ---------------------------------------------------------------------------


def _catalog(skills: Sequence[Skill], max_chars: int) -> str:
    lines = ["<available_skills>"]
    used = len(lines[0])
    omitted = 0
    for skill in skills:
        block = [
            "<skill>",
            f"<name>{html.escape(skill.name)}</name>",
            f"<description>{html.escape(skill.description, quote=False)}</description>",
        ]
        if skill.allowed_tools:
            block.append(
                "<allowed-tools>"
                f"{html.escape(skill.allowed_tools, quote=False)}"
                "</allowed-tools>"
            )
        block.append("</skill>")
        rendered = "\n".join(block)
        if max_chars and used + len(rendered) + 1 > max_chars:
            omitted += 1
            continue
        lines.append(rendered)
        used += len(rendered) + 1
    lines.append("</available_skills>")
    if omitted:
        lines.append(f"({omitted} more skills omitted to fit the context budget)")
    return "\n".join(lines)


def _format_files(label: str, files: Sequence[str]) -> str:
    if not files:
        return ""
    return f"{label}: {', '.join(files)}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class _Registry:
    """Shared state behind the four skill tools."""

    def __init__(
        self,
        skills: Sequence[Skill],
        errors: Sequence[str],
        sandbox: ScriptSandbox | None,
        *,
        max_resource_chars: int,
    ) -> None:
        self.skills = tuple(skills)
        self.errors = tuple(errors)
        self.sandbox = sandbox
        self.max_resource_chars = max_resource_chars
        self._by_name = {skill.name: skill for skill in skills}

    def get(self, name: str) -> Skill:
        try:
            return self._by_name[name]
        except KeyError:
            available = ", ".join(sorted(self._by_name)) or "none"
            raise KeyError(
                f"Unknown skill {name!r}. Available skills: {available}"
            ) from None

    # -- list ---------------------------------------------------------------

    def list_text(self) -> str:
        if not self.skills:
            return "No valid skills were found in the configured skill folders."
        lines = ["Available skills:", ""]
        for skill in self.skills:
            lines.append(f"**{skill.name}**")
            lines.append(f"  {skill.description}")
            if skill.allowed_tools:
                lines.append(f"  allowed-tools: {skill.allowed_tools}")
            for label, files in (
                ("scripts", skill.scripts()),
                ("references", skill.references()),
                ("assets", skill.assets()),
            ):
                row = _format_files(label, files)
                if row:
                    lines.append(f"  {row}")
            lines.append("")
        if self.errors:
            lines.append(f"Skipped {len(self.errors)} invalid skill(s):")
            lines.extend(f"  {error}" for error in self.errors)
        return "\n".join(lines).rstrip()

    def list_records(self) -> list[dict[str, Any]]:
        return [
            {
                "name": skill.name,
                "description": skill.description,
                "allowed_tools": skill.allowed_tools,
                "scripts": skill.scripts(),
                "references": skill.references(),
                "assets": skill.assets(),
            }
            for skill in self.skills
        ]

    # -- activate -----------------------------------------------------------

    def activate_text(self, name: str) -> str:
        try:
            skill = self.get(name)
            instructions = skill.instructions()
        except (KeyError, OSError, ValueError) as exc:
            return f"Error: {_message(exc)}"
        parts = [f"# Skill {skill.name!r} activated", ""]
        parts.append(instructions or "(This SKILL.md has no instructions.)")
        bundled = [
            _format_files(label, files)
            for label, files in (
                ("scripts", skill.scripts()),
                ("references", skill.references()),
                ("assets", skill.assets()),
            )
            if files
        ]
        if bundled:
            parts += ["", "---", "## Bundled files", ""]
            parts.extend(bundled)
        if skill.allowed_tools:
            parts += ["", f"Declared tools (allowed-tools): {skill.allowed_tools}"]
        return "\n".join(parts)

    def activate_record(self, name: str) -> dict[str, Any]:
        try:
            skill = self.get(name)
            return {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
                "instructions": skill.instructions(),
                "allowed_tools": skill.allowed_tools,
                "scripts": skill.scripts(),
                "references": skill.references(),
                "assets": skill.assets(),
                "license": skill.license,
                "compatibility": skill.compatibility,
                "metadata": dict(skill.metadata),
            }
        except (KeyError, OSError, ValueError) as exc:
            return {"error": _message(exc)}

    # -- read ---------------------------------------------------------------

    def read_text(self, name: str, path: str) -> str:
        try:
            skill = self.get(name)
            target = skill.resource(path)
            content, truncated = _read_text(target, self.max_resource_chars)
        except (KeyError, OSError, ValueError) as exc:
            return f"Error: {_message(exc)}"
        relative = target.relative_to(skill.path.resolve()).as_posix()
        body = f"# {relative}\n\n{content}"
        if truncated:
            body += f"\n\n[truncated at {self.max_resource_chars} characters]"
        return body

    def read_record(self, name: str, path: str) -> dict[str, Any]:
        try:
            skill = self.get(name)
            target = skill.resource(path)
            content, truncated = _read_text(target, self.max_resource_chars)
        except (KeyError, OSError, ValueError) as exc:
            return {"path": path, "error": _message(exc)}
        return {
            "path": target.relative_to(skill.path.resolve()).as_posix(),
            "content": content,
            "truncated": truncated,
        }

    # -- run ----------------------------------------------------------------

    def run_text(self, name: str, script: str, args: Any = None) -> str:
        try:
            result = self._run(name, script, args)
        except (KeyError, OSError, PermissionError, ValueError) as exc:
            return f"Error: {_message(exc)}"
        return result.output

    def run_record(self, name: str, script: str, args: Any = None) -> dict[str, Any]:
        try:
            result = self._run(name, script, args)
        except (KeyError, OSError, PermissionError, ValueError) as exc:
            return {"script": script, "error": _message(exc)}
        return {
            "script": script,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "elapsed": round(result.elapsed, 3),
            "output": result.output,
            "truncated": result.truncated,
            "sandbox": result.backend,
        }

    def _run(self, name: str, script: str, args: Any):
        if self.sandbox is None:
            raise ValueError("Script execution is disabled for this Skills() catalog")
        skill = self.get(name)
        scripts_dir = (skill.path / "scripts").resolve()
        if not scripts_dir.is_dir():
            raise FileNotFoundError(f"Skill {name!r} has no scripts/ directory")
        if not script or not script.strip():
            raise ValueError("A script name is required")
        relative = Path(script)
        if relative.is_absolute():
            raise ValueError(f"Script path must be relative: {script}")
        target = (scripts_dir / relative).resolve()
        try:
            target.relative_to(scripts_dir)
        except ValueError:
            raise ValueError(f"Script path escapes {name}/scripts/: {script}") from None
        if not target.is_file():
            available = ", ".join(_files(scripts_dir)) or "none"
            raise FileNotFoundError(
                f"Script {script!r} not found. Available scripts: {available}"
            )
        return self.sandbox.run(target, _argv(args), skill_dir=skill.path)


def _message(exc: Exception) -> str:
    return exc.args[0] if exc.args else str(exc)


def _argv(args: Any) -> list[str]:
    """Normalize the model's argv argument to a list of strings."""
    if args is None:
        return []
    if isinstance(args, Sequence) and not isinstance(args, (str, bytes)):
        return [str(item) for item in args]
    raise ValueError("args must be a list of strings")


def _read_text(path: Path, limit: int) -> tuple[str, bool]:
    if path.suffix.casefold() in _BINARY_SUFFIXES:
        raise ValueError(f"{path.name} looks binary; open it with a file tool instead")
    try:
        content = path.read_text()
    except UnicodeDecodeError:
        raise ValueError(
            f"{path.name} is not UTF-8 text; open it with a file tool instead"
        ) from None
    if limit and len(content) > limit:
        return content[:limit], True
    return content, False


# ---------------------------------------------------------------------------
# Tool construction
# ---------------------------------------------------------------------------


def _tool_names(prefix: str) -> dict[str, str]:
    """Tool names for one ``name_prefix``."""
    return {
        "list_skills": f"{prefix}list_skills",
        "activate_skill": f"{prefix}activate_skill",
        "read_skill_resource": f"{prefix}read_skill_resource",
        "run_skill_script": f"{prefix}run_skill_script",
    }


def _build_tools(
    registry: _Registry,
    *,
    mode: Literal["search", "code"],
    names: Mapping[str, str],
    scripts: bool,
    tool_cls: Any,
) -> list[Tool]:
    def list_skills() -> Any:
        """List the skills installed in the configured skill folders."""
        return registry.list_text() if mode == "search" else registry.list_records()

    def activate_skill(skill: str) -> Any:
        """Load one skill's full SKILL.md instructions."""
        return (
            registry.activate_text(skill)
            if mode == "search"
            else registry.activate_record(skill)
        )

    def read_skill_resource(skill: str, path: str) -> Any:
        """Read one reference or asset file bundled with a skill."""
        return (
            registry.read_text(skill, path)
            if mode == "search"
            else registry.read_record(skill, path)
        )

    def run_skill_script(skill: str, script: str, args: list[str] | None = None) -> Any:
        """Run one script from a skill's scripts/ folder."""
        return (
            registry.run_text(skill, script, args)
            if mode == "search"
            else registry.run_record(skill, script, args)
        )

    if mode == "search":
        list_desc, activate_desc, read_desc, run_desc = (
            _LIST_DESC,
            _ACTIVATE_DESC,
            _READ_DESC,
            _RUN_DESC,
        )
    else:
        list_desc, activate_desc, read_desc, run_desc = (
            _CODE_LIST_DESC,
            _CODE_ACTIVATE_DESC,
            _CODE_READ_DESC,
            _CODE_RUN_DESC,
        )

    tools: list[Tool] = [
        tool_cls(list_skills, name=names["list_skills"], desc=list_desc),
        tool_cls(activate_skill, name=names["activate_skill"], desc=activate_desc),
        tool_cls(
            read_skill_resource,
            name=names["read_skill_resource"],
            desc=read_desc,
        ),
    ]
    if scripts:
        tools.append(
            tool_cls(
                run_skill_script,
                name=names["run_skill_script"],
                desc=run_desc,
            )
        )
    return tools


# ---------------------------------------------------------------------------
# Public list
# ---------------------------------------------------------------------------


class SkillTools(list):
    """List of ``dspy.Tool`` objects backed by one or more skill folders.

    Behaves like a normal list, so it can be passed as ``tools=`` or
    concatenated with other tools. ``skills`` holds the discovered
    :class:`Skill` objects and ``errors`` the skipped directories.

    :meth:`patch` appends these tools to an existing ``dspy.ReAct``,
    ``dspy.ReActV2``, or ``dspy.RLM`` agent and injects the skill catalog
    into the instructions the model sees.
    """

    def __init__(
        self,
        tools: Sequence[Any],
        *,
        skills: Sequence[Skill],
        errors: Sequence[str],
        catalog: str,
        scripts: bool,
        names: Mapping[str, str] | None = None,
        mode: Literal["search", "code"] = "search",
    ) -> None:
        super().__init__(tools)
        self.skills = tuple(skills)
        self.errors = tuple(errors)
        self.scripts = scripts
        self._mode = mode
        self._catalog = catalog
        names = dict(names) if names is not None else _tool_names("")
        discovered = {tool.name for tool in tools}
        present = {key: value for key, value in names.items() if value in discovered}
        self._names = present or names

    def catalog(self) -> str:
        """The ``<available_skills>`` XML block injected by :meth:`patch`."""
        return self._catalog

    def _guidance(self) -> str:
        block = _GUIDANCE if self.scripts else _GUIDANCE_NO_SCRIPTS
        return block.format(**self._names)

    def _execution_note(self, tools: Sequence[Any]) -> str:
        template = _EXECUTION_NOTE if self.scripts else _EXECUTION_NOTE_NO_SCRIPTS
        parts = [template.format(**self._names)]
        if self._mode == "code" and self.scripts:
            parts.append(_REPL_NOTE.format(**self._names))
        found = _host_command_tools(tools)
        if found:
            parts.append(
                _HOST_TOOLS_NOTE.format(names=", ".join(f"`{name}`" for name in found))
            )
        return "\n\n".join(parts)

    def instructions(self, tools: Sequence[Any] = ()) -> str:
        """Guidance, execution rules, and the catalog for an agent with ``tools``.

        Pass the agent's full tool list so the model is told which host
        commands it can actually run (see :func:`_host_command_tools`).
        """
        return "\n\n".join(
            [self._guidance(), self._execution_note(tools), self._catalog]
        )

    def patch(self, agent: Any) -> Any:
        """Append the skill tools to ``agent`` and inject the catalog.

        The agent is reinitialized in place: its signature gets the skill
        guidance, the execution rules, and the ``<available_skills>`` catalog
        appended to its instructions, and its tool list gains the four skill
        tools. The execution rules name the tools the agent already has that
        can run host commands, so a skill whose instructions call a CLI is
        either followed for real or reported as a command the user must run;
        the model is told never to invent command output.

        Works for ``dspy.ReAct``, ``dspy.ReActV2``, and ``dspy.RLM``; other
        module types raise ``TypeError``.

        Calling ``patch`` twice on the same agent is a no-op. Patching an
        agent that already received these tools through ``tools=`` only adds
        the catalog.

        Returns the agent for chaining: ``agent = Skills(...).patch(agent)``.
        """
        import dspy

        if not hasattr(agent, "signature"):
            raise TypeError(
                f"patch() needs a dspy module with a signature, got {type(agent).__name__}"
            )

        marker = "_dspy_agent_tools_skills"
        already = getattr(agent, marker, None)
        if already is self:
            return agent
        if already is not None:
            raise ValueError(
                "This agent is already patched with a different Skills() "
                "catalog; build one Skills(...) with every skill folder."
            )

        if isinstance(agent, getattr(dspy, "RLM", ())):
            kind = "RLM"
        elif isinstance(agent, getattr(dspy, "ReActV2", ())):
            kind = "ReActV2"
        elif isinstance(agent, dspy.ReAct):
            kind = "ReAct"
        else:
            raise TypeError(
                "patch() supports dspy.ReAct, dspy.ReActV2, and dspy.RLM; "
                f"got {type(agent).__name__}"
            )
        existing = _agent_tools(agent, kind)
        additions = []
        for tool in self:
            found = existing.get(tool.name)
            if found is None:
                additions.append(tool)
            elif found is not tool:
                raise ValueError(
                    f"Agent already has a different tool named {tool.name!r}; "
                    "pass name_prefix=... to Skills() to avoid the collision."
                )

        instructions = agent.signature.instructions
        block = self.instructions([*existing.values(), *additions])
        signature = agent.signature.with_instructions(
            f"{instructions}\n\n{block}" if instructions else block
        )

        if kind == "ReAct":
            tools = [tool for name, tool in existing.items() if name != "finish"]
            rebuilt = dspy.ReAct(
                signature, tools=[*tools, *additions], max_iters=agent.max_iters
            )
        elif kind == "ReActV2":
            tools = [tool for name, tool in existing.items() if name != "submit"]
            rebuilt = dspy.ReActV2(
                signature, tools=[*tools, *additions], max_iters=agent.max_iters
            )
        elif kind == "RLM":
            rebuilt = dspy.RLM(
                signature,
                max_iters=agent.max_iters,
                max_llm_calls=agent.max_llm_calls,
                max_output_chars=agent.max_output_chars,
                verbose=agent.verbose,
                tools=[*existing.values(), *additions],
                sub_lm=agent.sub_lm,
                interpreter_factory=agent._interpreter_factory,
            )
        _copy_module_state(agent, rebuilt)
        setattr(agent, marker, self)
        return agent


_MODULE_STATE = frozenset({"callbacks", "history", "_compiled"})


def _copy_module_state(agent: Any, rebuilt: Any) -> None:
    """Copy a rebuilt module's own state onto ``agent`` in place.

    Module-level bookkeeping (callbacks, history, compile flag) is preserved
    from the original instance.
    """
    for name, value in vars(rebuilt).items():
        if name not in _MODULE_STATE:
            setattr(agent, name, value)


def _agent_tools(agent: Any, kind: str) -> dict[str, Any]:
    """Return the agent's tool mapping keyed by name."""
    if kind == "RLM":
        return dict(getattr(agent, "_user_tools", {}))
    tools = getattr(agent, "tools", None)
    if not isinstance(tools, Mapping):
        raise TypeError(f"Unsupported {kind} tool container: {type(tools).__name__}")
    return dict(tools)


def _host_command_tools(tools: Sequence[Any]) -> list[str]:
    """Names of the tools that can run a host command.

    A tool counts when it comes from :mod:`dspy_agent_tools.bash` or
    :mod:`dspy_agent_tools.cli`, or when its name matches a common shell
    spelling, so the model is told which commands it can actually run.
    """
    found = set()
    for tool in tools:
        func = getattr(tool, "func", None)
        name = getattr(tool, "name", None) or getattr(func, "__name__", "")
        module = getattr(func, "__module__", "") or ""
        if (
            module in _HOST_TOOL_MODULES
            or name in _SHELL_TOOL_NAMES
            or name.endswith(_SHELL_TOOL_SUFFIXES)
        ):
            found.add(str(name))
    return sorted(name for name in found if name)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def Skills(
    *roots: str | Path,
    mode: Literal["search", "code"] = "search",
    name_prefix: str = "",
    strict: bool = False,
    scripts: bool = True,
    sandbox: SandboxName = "auto",
    timeout: float = DEFAULT_SCRIPT_TIMEOUT,
    allow_network: bool = False,
    allow_write: bool = False,
    interpreters: Sequence[str] = DEFAULT_INTERPRETERS,
    env: Mapping[str, str] | None = None,
    max_output_chars: int = 0,
    max_resource_chars: int = DEFAULT_MAX_RESOURCE_CHARS,
    max_catalog_chars: int = DEFAULT_MAX_CATALOG_CHARS,
) -> SkillTools:
    """Load Agent Skills folders and expose them as DSPy tools.

    Each root is scanned for skill directories: a folder containing a
    ``SKILL.md`` with ``name`` and ``description`` frontmatter. A root that is
    itself a skill is accepted too. Invalid skills are skipped and recorded
    on ``SkillTools.errors`` unless ``strict=True``.

    Args:
        roots: One or more folders that house the skills, for example
            ``Skills("~/.agents/skills", ".agents/skills")``.
        mode: ``"search"`` (default) returns text for ``dspy.ReAct`` and
            ``dspy.ReActV2``; ``"code"`` returns dictionaries for
            ``dspy.RLM``.
        name_prefix: Prepended to every tool name, for example
            ``name_prefix="skill_"``. It must form a valid Python identifier,
            so ``"sk-"`` is rejected and ``"sk_"`` is not.
        strict: Raise ``ValueError`` for the first invalid skill instead of
            skipping it.
        scripts: Include ``run_skill_script``. When ``False``, no script is
            ever executed and the sandbox is not configured.
        sandbox: OS sandbox for scripts: ``"auto"`` (default) uses bwrap,
            firejail, or sandbox-exec when installed and falls back to
            process confinement; ``"required"`` refuses the fallback; a
            backend name forces one; ``"off"`` skips the OS layer.
        timeout: Script deadline in seconds (default 30). The whole process
            group is killed when it expires.
        allow_network: Let scripts reach the network. Off by default; the
            OS sandboxes deny it.
        allow_write: Let scripts write inside the skill folder. Off by
            default; a throwaway temp directory is always writable.
        interpreters: Interpreter families scripts may use. Defaults to
            python, bash, sh, node, deno, ruby, and perl.
        env: Extra environment variables for scripts. The environment is
            replaced, not extended, so ambient secrets are never inherited.
        max_output_chars: Cap one script result (``0`` keeps everything).
        max_resource_chars: Cap one ``read_skill_resource`` result
            (``0`` keeps everything).
        max_catalog_chars: Cap the ``<available_skills>`` block injected by
            ``patch`` (``0`` keeps every skill).

    Returns:
        A :class:`SkillTools` list with ``list_skills``, ``activate_skill``,
        ``read_skill_resource``, and (unless ``scripts=False``)
        ``run_skill_script``.

    Examples:
        >>> from dspy_agent_tools import Skills
        >>> skills = Skills("~/.agents/skills")
        >>> agent = dspy.ReAct("question -> answer", tools=skills)

        >>> agent = dspy.ReAct("question -> answer", tools=[bash_exec])
        >>> Skills("./skills").patch(agent)

        >>> rlm = dspy.RLM("task -> report", tools=Skills("./skills", mode="code"))
    """
    if mode not in _MODES:
        raise ValueError(f"Unknown mode {mode!r}: expected 'search' or 'code'")
    if not roots:
        raise ValueError("Skills() needs at least one skill folder")
    if max_resource_chars < 0:
        raise ValueError("max_resource_chars must be >= 0")
    if max_catalog_chars < 0:
        raise ValueError("max_catalog_chars must be >= 0")

    paths = [Path(root).expanduser() for root in roots]
    names = _tool_names(name_prefix)
    if scripts is False:
        names.pop("run_skill_script")
    invalid = sorted(name for name in names.values() if not name.isidentifier())
    if invalid:
        raise ValueError(
            "name_prefix must form valid Python identifiers for every tool name; "
            f"got {name_prefix!r} producing {invalid[0]!r}"
        )
    tool_cls = _load_tool()
    discovered, errors = _discover(paths, strict=strict)
    sandbox_runner = (
        ScriptSandbox(
            sandbox=sandbox,
            timeout=timeout,
            allow_network=allow_network,
            allow_write=allow_write,
            interpreters=interpreters,
            env=env,
            max_output_chars=max_output_chars,
        )
        if scripts
        else None
    )
    registry = _Registry(
        discovered,
        errors,
        sandbox_runner,
        max_resource_chars=max_resource_chars,
    )
    catalog = _catalog(discovered, max_catalog_chars)
    tools = _build_tools(
        registry,
        mode=mode,
        names=names,
        scripts=scripts,
        tool_cls=tool_cls,
    )
    return SkillTools(
        tools,
        skills=discovered,
        errors=errors,
        catalog=catalog,
        scripts=scripts,
        names=names,
        mode=mode,
    )


__all__ = ["Skill", "SkillTools", "Skills"]
