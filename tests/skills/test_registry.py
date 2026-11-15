"""Tests for dspy_agent_tools.skills.registry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dspy_agent_tools import Patch, Skills
from dspy_agent_tools.patch import _agent_tools
from dspy_agent_tools.skills.registry import _argv, _Registry

from .helpers import SKILLS, by_name


class TestListAndActivate:
    def test_list_text_mentions_skills_and_scripts(self):
        text = by_name(Skills(SKILLS))["list_skills"]()

        assert "**demo-skill**" in text
        assert "Demonstrate the Agent Skills layout" in text
        assert "scripts: escape.py" in text
        assert "**second-skill**" in text
        assert "Skipped 2 invalid skill(s)" in text

    def test_list_code_returns_records(self):
        records = by_name(Skills(SKILLS, mode="code"))["list_skills"]()

        assert [record["name"] for record in records] == ["demo-skill", "second-skill"]
        assert "greet.py" in records[0]["scripts"]
        assert records[0]["allowed_tools"] == "Bash(git:*) Bash(jq:*)"

    def test_activate_returns_instructions_and_files(self):
        tools = by_name(Skills(SKILLS))

        text = tools["activate_skill"](skill="demo-skill")

        assert "# Skill 'demo-skill' activated" in text
        assert "Follow these steps to use the demo skill." in text
        assert "scripts: escape.py" in text
        assert "references: guide.md" in text
        assert "assets: template.txt" in text
        assert "Declared tools (allowed-tools): Bash(git:*) Bash(jq:*)" in text
        assert "name:" not in text  # frontmatter is not part of the instructions

    def test_activate_code_returns_a_record(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["activate_skill"](skill="second-skill")

        assert record["name"] == "second-skill"
        assert record["instructions"] == "Follow the second skill instructions."
        assert record["scripts"] == []

    def test_activate_code_surfaces_frontmatter(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["activate_skill"](skill="demo-skill")

        assert record["allowed_tools"] == "Bash(git:*) Bash(jq:*)"
        assert record["license"] == "MIT"
        assert record["compatibility"] == "Requires Python 3.13+"
        assert record["metadata"] == {"author": "dspy-agent-tools"}

    def test_unknown_skill_lists_available_names(self):
        tools = by_name(Skills(SKILLS))

        assert "Unknown skill 'nope'" in tools["activate_skill"](skill="nope")
        assert "demo-skill" in tools["activate_skill"](skill="nope")


class TestReadResource:
    def test_reads_references_and_assets(self):
        tools = by_name(Skills(SKILLS))

        guide = tools["read_skill_resource"](
            skill="demo-skill", path="references/guide.md"
        )
        asset = tools["read_skill_resource"](
            skill="demo-skill", path="assets/template.txt"
        )

        assert "The guide explains the demo skill" in guide
        assert guide.startswith("# references/guide.md")
        assert "template content" in asset

    def test_rejects_path_escapes(self):
        tools = by_name(Skills(SKILLS))

        escape = tools["read_skill_resource"](
            skill="demo-skill", path="../second-skill/SKILL.md"
        )
        absolute = tools["read_skill_resource"](skill="demo-skill", path="/etc/passwd")

        assert "escapes the skill directory" in escape
        assert "must be relative" in absolute

    def test_truncates_large_resources(self):
        tools = by_name(Skills(SKILLS, max_resource_chars=10))

        text = tools["read_skill_resource"](
            skill="demo-skill", path="references/guide.md"
        )

        assert "[truncated at 10 characters]" in text

    def test_code_mode_reports_missing_files(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["read_skill_resource"](
            skill="demo-skill", path="references/nope.md"
        )

        assert "error" in record


class TestCatalogAndCodeMode:
    def test_catalog_budget_omits_skills(self):
        tools = Skills(SKILLS, max_catalog_chars=10)

        assert "more skills omitted" in tools.catalog()

    def test_catalog_declares_allowed_tools(self):
        tools = Skills(SKILLS)

        assert (
            "<allowed-tools>Bash(git:*) Bash(jq:*)</allowed-tools>" in tools.catalog()
        )

    def test_section_drops_script_guidance_when_disabled(self):
        text = Skills(SKILLS, scripts=False).section()

        assert "script running is disabled" in text
        assert "run_skill_script" not in text

    def test_section_names_the_script_tool(self):
        text = Skills(SKILLS).section()

        assert "`run_skill_script`" in text
        assert "<available_skills>" in text

    def test_code_mode_activate_reports_unknown_skills(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        assert "error" in tools["activate_skill"](skill="nope")

    def test_code_mode_reads_resources(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["read_skill_resource"](
            skill="demo-skill", path="references/guide.md"
        )

        assert record["content"].startswith("# Guide")
        assert record["truncated"] is False

    def test_code_mode_run_reports_errors(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        assert "error" in tools["run_skill_script"](
            skill="demo-skill", script="nope.py"
        )

    def test_disabled_script_execution_reports_an_error(self):
        catalog = Skills(SKILLS, scripts=False)
        registry = _Registry(
            catalog.skills, catalog.errors, None, max_resource_chars=100
        )

        assert "disabled" in registry.run_text("demo-skill", "greet.py")

    def test_argv_rejects_non_sequences(self):
        with pytest.raises(ValueError, match="args must be"):
            _argv(123)

    def test_binary_and_non_utf8_resources_are_rejected(self, tmp_path):
        skill_dir = tmp_path / "s"
        (skill_dir / "assets").mkdir(parents=True)
        (skill_dir / "assets" / "image.png").write_bytes(b"\x89PNG")
        (skill_dir / "assets" / "latin1.txt").write_bytes(b"caf\xe9")
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n")
        tools = by_name(Skills(tmp_path))

        binary = tools["read_skill_resource"](skill="s", path="assets/image.png")
        latin = tools["read_skill_resource"](skill="s", path="assets/latin1.txt")

        assert "binary" in binary
        assert "not UTF-8" in latin

    def test_patch_requires_a_dspy_module(self):
        with pytest.raises(TypeError, match="signature"):
            Patch(object(), tools=Skills(SKILLS))

    def test_unsupported_tool_containers_are_rejected(self):
        with pytest.raises(TypeError, match="Unsupported ReAct tool container"):
            _agent_tools(SimpleNamespace(tools=None), "ReAct")

    def test_negative_limits_are_rejected(self):
        with pytest.raises(ValueError, match="max_resource_chars"):
            Skills(SKILLS, max_resource_chars=-1)
        with pytest.raises(ValueError, match="max_catalog_chars"):
            Skills(SKILLS, max_catalog_chars=-1)
