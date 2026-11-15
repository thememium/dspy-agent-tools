"""Tests for dspy_agent_tools.skills.skill."""

from __future__ import annotations

from pathlib import Path

import pytest

import dspy_agent_tools.skills.skill as skill_module
from dspy_agent_tools import Skill, Skills
from dspy_agent_tools.skills.skill import (
    _discover,
    _load_skill,
    _validate,
    _validate_name,
)

from .helpers import SKILLS, by_name


class TestDiscovery:
    def test_loads_valid_skills_and_skips_invalid_ones(self):
        tools = Skills(SKILLS)

        assert [skill.name for skill in tools.skills] == ["demo-skill", "second-skill"]
        assert len(tools.errors) == 2
        assert any(
            "InvalidName" in error or "invalid-name" in error for error in tools.errors
        )
        assert any("description" in error for error in tools.errors)

    def test_strict_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid skills"):
            Skills(SKILLS, strict=True)

    def test_accepts_a_root_that_is_a_skill(self):
        tools = Skills(SKILLS / "demo-skill")

        assert [skill.name for skill in tools.skills] == ["demo-skill"]

    def test_missing_root_is_reported_not_raised(self):
        tools = Skills("/definitely/not/a/skill/folder")

        assert tools.skills == ()
        assert any("not a directory" in error for error in tools.errors)
        assert "No valid skills" in by_name(tools)["list_skills"]()

    def test_skill_metadata_is_parsed(self):
        skill = Skills(SKILLS).skills[0]

        assert isinstance(skill, Skill)
        assert skill.license == "MIT"
        assert skill.compatibility == "Requires Python 3.13+"
        assert skill.allowed_tools == "Bash(git:*) Bash(jq:*)"
        assert skill.metadata == {"author": "dspy-agent-tools"}
        assert skill.scripts() == [
            "escape.py",
            "fail.py",
            "greet.py",
            "loud.py",
            "slow.py",
            "write_here.py",
        ]
        assert skill.references() == ["guide.md"]
        assert skill.assets() == ["template.txt"]


class TestFrontmatterAndValidation:
    def test_skill_md_falls_back_to_canonical_name(self, tmp_path):
        skill = Skill(name="s", description="d", path=tmp_path)

        assert skill.skill_md == tmp_path / "SKILL.md"

    def test_empty_resource_path_is_rejected(self, tmp_path):
        skill = Skill(name="s", description="d", path=tmp_path)

        with pytest.raises(ValueError, match="resource path"):
            skill.resource("")

    def test_name_rules(self, tmp_path):
        assert _validate_name("", tmp_path) == [
            "Field 'name' must be a non-empty string"
        ]
        assert any("64" in p for p in _validate_name("a" * 65, tmp_path / ("a" * 65)))
        assert any(
            "lowercase" in p for p in _validate_name("Upper", tmp_path / "Upper")
        )
        assert any("start or end" in p for p in _validate_name("-x", tmp_path / "-x"))
        assert any(
            "consecutive" in p for p in _validate_name("a--b", tmp_path / "a--b")
        )
        assert any(
            "letters, digits" in p for p in _validate_name("a_b", tmp_path / "a_b")
        )

    def test_description_and_compatibility_rules(self, tmp_path):
        problems = _validate(Skill(name="s", description="", path=tmp_path))
        assert any("description" in p for p in problems)

        problems = _validate(Skill(name="s", description="d" * 1025, path=tmp_path))
        assert any("1024" in p for p in problems)

        problems = _validate(
            Skill(name="s", description="d", path=tmp_path, compatibility="c" * 501)
        )
        assert any("500" in p for p in problems)

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ("no frontmatter\n", ValueError),
            ("---\nname: x\n", ValueError),
            ("---\nname: [oops\n---\n", ValueError),
            ("---\n- a\n- b\n---\n", TypeError),
            ("---\nname: ''\ndescription: d\n---\n", ValueError),
            ("---\nname: x\ndescription: d\nmetadata:\n  - a\n---\n", TypeError),
            (
                "---\nname: x\ndescription: d\nallowed-tools:\n  - Bash\n---\n",
                TypeError,
            ),
            ("---\nname: x\ndescription: d\nlicense: 3\n---\n", TypeError),
            ("---\nname: x\ndescription: d\ncompatibility: 3\n---\n", TypeError),
        ],
    )
    def test_load_skill_rejects_bad_frontmatter(
        self, tmp_path: Path, text: str, error: type[Exception]
    ):
        skill_dir = tmp_path / "x"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(text)

        with pytest.raises(error):
            _load_skill(skill_dir)

    def test_load_skill_requires_a_skill_md(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()

        with pytest.raises(ValueError, match="not found"):
            _load_skill(empty)

    def test_files_skips_directories_hidden_files_and_escaping_symlinks(self, tmp_path):
        skill_dir = tmp_path / "s"
        scripts = skill_dir / "scripts"
        (scripts / "sub").mkdir(parents=True)
        (scripts / "sub" / "nested.py").write_text("x")
        (scripts / ".hidden").write_text("x")
        (scripts / "__pycache__").mkdir()
        (scripts / "__pycache__" / "x.pyc").write_text("x")
        outside = tmp_path / "outside.py"
        outside.write_text("x")
        (scripts / "link.py").symlink_to(outside)
        skill = Skill(name="s", description="d", path=skill_dir)

        assert skill.scripts() == ["sub/nested.py"]


class TestDiscoveryEdgeCases:
    def test_unreadable_root_is_reported(self, monkeypatch, tmp_path):
        def boom(root: Path) -> list[Path]:
            raise OSError("boom")

        monkeypatch.setattr(skill_module, "_skill_dirs", boom)

        skills, errors = _discover([tmp_path], strict=False)

        assert skills == []
        assert any("boom" in error for error in errors)

    def test_ignores_subdirectories_without_a_skill_md(self, tmp_path):
        root = tmp_path / "skills"
        (root / "notes").mkdir(parents=True)
        (root / "notes" / "README.md").write_text("x")

        assert _discover([root], strict=False) == ([], [])

    def test_duplicate_names_across_roots_are_reported(self, tmp_path):
        for root_name in ("one", "two"):
            skill_dir = tmp_path / root_name / "dup"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: dup\ndescription: d\n---\n")

        skills, errors = _discover([tmp_path / "one", tmp_path / "two"], strict=False)

        assert [skill.name for skill in skills] == ["dup"]
        assert any("duplicate skill name" in error for error in errors)
