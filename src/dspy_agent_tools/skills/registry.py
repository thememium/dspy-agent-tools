"""Shared registry and helpers behind the skill tools."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .skill import _BINARY_SUFFIXES, Skill, _files, _format_files

if TYPE_CHECKING:
    from .sandbox import ScriptSandbox


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
