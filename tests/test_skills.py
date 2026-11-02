"""Tests for dspy_agent_tools.skills and its script sandbox."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, cast, get_args

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import dspy_agent_tools.sandbox as sandbox_module
from dspy_agent_tools import Skill, Skills, SkillTools
from dspy_agent_tools.sandbox import (
    ScriptSandbox,
    bwrap_argv,
    firejail_argv,
    resolve_backend,
    sandbox_exec_profile,
)

SKILLS = Path(__file__).parent / "fixtures" / "skills"


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
        assert skill.metadata == {"author": "dspy-agent-tools"}
        assert skill.scripts() == [
            "escape.py",
            "fail.py",
            "greet.py",
            "slow.py",
            "write_here.py",
        ]
        assert skill.references() == ["guide.md"]
        assert skill.assets() == ["template.txt"]


class TestToolSurface:
    def test_default_tools(self):
        tools = Skills(SKILLS)

        assert isinstance(tools, SkillTools)
        assert set(by_name(tools)) == {
            "list_skills",
            "activate_skill",
            "read_skill_resource",
            "run_skill_script",
        }

    def test_scripts_can_be_disabled(self):
        tools = Skills(SKILLS, scripts=False)

        assert set(by_name(tools)) == {
            "list_skills",
            "activate_skill",
            "read_skill_resource",
        }

    def test_name_prefix(self):
        tools = Skills(SKILLS, name_prefix="skill_")

        assert "skill_list_skills" in by_name(tools)

    def test_rejects_unknown_mode_and_empty_roots(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            Skills(SKILLS, mode=cast(Any, "tools"))
        with pytest.raises(ValueError, match="at least one skill folder"):
            Skills()


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

    def test_activate_returns_instructions_and_files(self):
        tools = by_name(Skills(SKILLS))

        text = tools["activate_skill"](skill="demo-skill")

        assert "# Skill 'demo-skill' activated" in text
        assert "Follow these steps to use the demo skill." in text
        assert "scripts: escape.py" in text
        assert "references: guide.md" in text
        assert "assets: template.txt" in text
        assert "name:" not in text  # frontmatter is not part of the instructions

    def test_activate_code_returns_a_record(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["activate_skill"](skill="second-skill")

        assert record["name"] == "second-skill"
        assert record["instructions"] == "Follow the second skill instructions."
        assert record["scripts"] == []

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


class TestRunScript:
    def test_runs_a_script_with_arguments(self, monkeypatch):
        monkeypatch.setenv("SKILL_TEST_SECRET", "should-not-leak")
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](
            skill="demo-skill", script="greet.py", args=["hello", "world"]
        )

        assert "greet: hello world" in output
        assert "secret: <unset>" in output
        assert f"cwd: {SKILLS / 'demo-skill'}" in output
        assert output.rstrip().endswith("s)")

    def test_code_mode_returns_structured_result(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["run_skill_script"](
            skill="demo-skill", script="greet.py", args=["hi"]
        )

        assert record["exit_code"] == 0
        assert record["timed_out"] is False
        assert record["sandbox"] in {"bwrap", "firejail", "sandbox-exec", "process"}
        assert "greet: hi" in record["output"]

    def test_non_zero_exit_is_reported(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="fail.py")

        assert "about to fail" in output
        assert "[exit code 3]" in output

    def test_timeout_kills_the_script(self):
        tools = by_name(Skills(SKILLS, timeout=0.5))

        output = tools["run_skill_script"](skill="demo-skill", script="slow.py")

        assert "timed out after 0.5s" in output

    def test_missing_script_lists_alternatives(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="nope.py")

        assert "Script 'nope.py' not found" in output
        assert "greet.py" in output

    def test_rejects_scripts_outside_the_scripts_folder(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="../SKILL.md")
        absolute = tools["run_skill_script"](skill="demo-skill", script="/etc/passwd")

        assert "escapes demo-skill/scripts" in output
        assert "must be relative" in absolute

    def test_interpreter_allowlist(self):
        tools = by_name(Skills(SKILLS, interpreters=("bash",)))

        output = tools["run_skill_script"](skill="demo-skill", script="greet.py")

        assert "Interpreter 'python' is not allowed" in output

    def test_unknown_interpreter_extension(self, tmp_path):
        skill = copy_skill(tmp_path)
        (skill / "scripts" / "data.bin").write_bytes(b"\x00\x01")
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](skill="demo-skill", script="data.bin")

        assert "Cannot determine an interpreter" in output


@pytest.mark.skipif(resolve_backend("auto") is None, reason="no OS sandbox installed")
class TestSandboxEnforcement:
    def test_skill_folder_is_read_only_by_default(self, tmp_path):
        skill = copy_skill(tmp_path)
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](skill="demo-skill", script="write_here.py")

        assert "PermissionError" in output or "Operation not permitted" in output
        assert not (skill / "written-by-skill.txt").exists()

    def test_allow_write_permits_the_skill_folder(self, tmp_path):
        skill = copy_skill(tmp_path)
        tools = by_name(Skills(tmp_path, allow_write=True))

        output = tools["run_skill_script"](skill="demo-skill", script="write_here.py")

        assert "wrote written-by-skill.txt" in output
        assert (skill / "written-by-skill.txt").read_text() == "ok"

    def test_scripts_cannot_write_outside_the_skill(self, tmp_path):
        copy_skill(tmp_path)
        outside = tmp_path / "escaped.txt"
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](
            skill="demo-skill", script="escape.py", args=[str(outside)]
        )

        assert not outside.exists()
        assert "PermissionError" in output or "Operation not permitted" in output


class TestSandboxConstruction:
    def test_off_uses_process_confinement(self):
        assert resolve_backend("off") is None

    def test_unknown_sandbox_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown sandbox"):
            resolve_backend(cast(Any, "docker"))

    def test_unavailable_backend_raises(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        with pytest.raises(ValueError, match="not available"):
            resolve_backend("bwrap")

    def test_required_without_a_backend_raises(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        with pytest.raises(ValueError, match="no OS sandbox is installed"):
            resolve_backend("required")

    def test_bwrap_argv(self, tmp_path):
        argv = bwrap_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=False,
            allow_write=True,
        )

        assert argv[:2] == ["bwrap", "--unshare-all"]
        assert "--share-net" not in argv
        assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind") :][:3]
        assert ["--bind", str(tmp_path), str(tmp_path)] == argv[argv.index("--bind") :][
            :3
        ]
        assert argv[-3:] == ["--", "python", "s.py"]

    def test_firejail_argv(self, tmp_path):
        argv = firejail_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=False,
            allow_write=False,
        )

        assert argv[0] == "firejail"
        assert "--net=none" in argv
        assert "--read-only=/" in argv
        assert f"--working-directory={tmp_path}" in argv

    def test_sandbox_exec_profile(self, tmp_path):
        profile = sandbox_exec_profile(
            tmp_path,
            tmp_path / "tmp",
            allow_network=False,
            allow_write=False,
        )

        assert "(deny file-write*)" in profile
        assert "(deny network*)" in profile
        assert str((tmp_path / "tmp").resolve()) in profile

    def test_sandbox_requires_a_positive_timeout(self):
        with pytest.raises(ValueError, match="timeout"):
            ScriptSandbox(timeout=0)

    def test_script_outside_skill_is_rejected(self, tmp_path):
        sandbox = ScriptSandbox(sandbox="off")
        skill = copy_skill(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_text("print('x')")

        with pytest.raises(ValueError, match="outside the skill directory"):
            sandbox.run(outside, [], skill_dir=skill)

    def test_shebang_scripts_are_supported(self, tmp_path):
        skill = copy_skill(tmp_path)
        script = skill / "scripts" / "hello"
        script.write_text(f"#!{sys.executable}\nprint('shebang ok')\n")
        sandbox = ScriptSandbox(sandbox="off", interpreters=("python",))

        result = sandbox.run(script, [], skill_dir=skill)

        assert result.exit_code == 0
        assert "shebang ok" in result.output


class TestPatch:
    def test_patches_react_in_place(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        returned = tools.patch(agent)

        assert returned is agent
        assert set(cast(Any, agent.tools)) == {
            "list_skills",
            "activate_skill",
            "read_skill_resource",
            "run_skill_script",
            "finish",
        }
        choices = get_args(
            agent.react.signature.output_fields["next_tool_name"].annotation
        )
        assert "run_skill_script" in choices
        instructions = cast(Any, agent.react.signature).instructions
        assert "<available_skills>" in instructions
        assert agent.max_iters == 2

    def test_patches_react_v2(self):
        tools = Skills(SKILLS)
        agent = dspy.ReActV2(QA, tools=[], max_iters=2)

        tools.patch(agent)

        assert "run_skill_script" in cast(Any, agent.tools)
        assert "<available_skills>" in cast(Any, agent.react.signature).instructions

    def test_patches_rlm(self):
        tools = Skills(SKILLS)
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        tools.patch(agent)

        assert "activate_skill" in cast(Any, agent._user_tools)
        instructions = cast(Any, agent.generate_action.signature).instructions
        assert "<available_skills>" in instructions

    def test_patch_is_idempotent(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[])

        tools.patch(agent)
        before = dict(cast(Any, agent.tools))
        tools.patch(agent)

        assert dict(cast(Any, agent.tools)) == before
        instructions = cast(Any, agent.react.signature).instructions
        assert instructions.count("<available_skills>") == 1

    def test_patch_only_adds_catalog_when_tools_are_already_present(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=tools)

        tools.patch(agent)

        assert sum(name == "list_skills" for name in cast(Any, agent.tools)) == 1
        instructions = cast(Any, agent.react.signature).instructions
        assert "<available_skills>" in instructions

    def test_patch_rejects_foreign_tool_names(self):
        tools = Skills(SKILLS)
        foreign = dspy.Tool(lambda: "x", name="list_skills", desc="not ours")
        agent = dspy.ReAct(QA, tools=[foreign])

        with pytest.raises(ValueError, match="different tool named 'list_skills'"):
            tools.patch(agent)

    def test_patch_rejects_a_second_catalog(self):
        first = Skills(SKILLS)
        second = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[])

        first.patch(agent)
        with pytest.raises(ValueError, match="already patched"):
            second.patch(agent)

    def test_patch_rejects_unsupported_modules(self):
        agent = dspy.Predict("question -> answer")

        with pytest.raises(TypeError, match="supports dspy.ReAct"):
            Skills(SKILLS).patch(agent)

    def test_patched_react_loop_can_use_a_skill(self):
        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[], max_iters=3)
        tools.patch(agent)
        lm = DummyLM(
            [
                {
                    "next_thought": "load the skill",
                    "next_tool_name": "activate_skill",
                    "next_tool_args": {"skill": "demo-skill"},
                },
                {
                    "next_thought": "run its script",
                    "next_tool_name": "run_skill_script",
                    "next_tool_args": {
                        "skill": "demo-skill",
                        "script": "greet.py",
                        "args": ["dspy"],
                    },
                },
                {
                    "next_thought": "done",
                    "next_tool_name": "finish",
                    "next_tool_args": {},
                },
                {"reasoning": "the script ran", "answer": "ok"},
            ]
        )

        with dspy.context(lm=lm):
            prediction = agent(question="greet dspy")

        assert "Demo Skill" in prediction.trajectory["observation_0"]
        assert "greet: dspy" in prediction.trajectory["observation_1"]
        assert prediction.answer == "ok"
