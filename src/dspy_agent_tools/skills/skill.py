"""Skill model, frontmatter parsing, validation, and discovery."""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_COMPATIBILITY_CHARS = 500

DEFAULT_MAX_CATALOG_CHARS = 8_000
DEFAULT_MAX_RESOURCE_CHARS = 50_000

_SKILL_FILE = "SKILL.md"
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
