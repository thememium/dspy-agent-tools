"""Shared fakes and helpers for the skills test package."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import dspy

SKILLS = Path(__file__).parents[1] / "fixtures" / "skills"


class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


def by_name(tools: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in tools}


def copy_skill(tmp_path: Path, name: str = "demo-skill") -> Path:
    """Copy one fixture skill into a writable temp folder."""
    target = tmp_path / name
    shutil.copytree(SKILLS / name, target)
    return target
