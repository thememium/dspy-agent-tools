"""Tests for dspy_agent_tools.skills and its script sandbox."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args

import dspy
import pytest
from dspy.utils.dummies import DummyLM

import dspy_agent_tools.sandbox as sandbox_module
import dspy_agent_tools.skills as skills_module
from dspy_agent_tools import Skill, Skills, SkillTools, bash_exec, web_search
from dspy_agent_tools.sandbox import (
    ScriptSandbox,
    _family,
    bwrap_argv,
    firejail_argv,
    resolve_backend,
    sandbox_exec_profile,
)
from dspy_agent_tools.skills import (
    _agent_tools,
    _argv,
    _discover,
    _load_skill,
    _Registry,
    _validate,
    _validate_name,
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

    def test_name_prefix_must_form_identifiers(self):
        with pytest.raises(ValueError, match="valid Python identifiers"):
            Skills(SKILLS, name_prefix="sk-")

    def test_identifier_prefix_patches_rlm(self):
        agent = dspy.RLM(QA, tools=[])

        Skills(SKILLS, mode="code", name_prefix="sk_").patch(agent)

        assert "sk_activate_skill" in cast(Any, agent._user_tools)

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

    def test_skill_without_scripts_dir(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="second-skill", script="anything.py")

        assert "has no scripts/ directory" in output

    def test_empty_script_name(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="")

        assert "script name is required" in output

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

    def test_patch_forbids_inventing_command_output(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        Skills(SKILLS).patch(agent)

        instructions = cast(Any, agent.react.signature).instructions
        assert "never claim you ran a command" in instructions
        assert "never invent its output" in instructions
        assert "Host command tools available" not in instructions

    def test_patch_names_the_shell_tools_it_has(self):
        agent = dspy.ReAct(QA, tools=[bash_exec], max_iters=2)

        Skills(SKILLS).patch(agent)

        instructions = cast(Any, agent.react.signature).instructions
        assert "Host command tools available to you: `bash_exec`." in instructions

    def test_patch_names_fetch_tools_without_a_shell(self):
        agent = dspy.ReAct(QA, tools=[web_search], max_iters=2)

        Skills(SKILLS).patch(agent)

        instructions = cast(Any, agent.react.signature).instructions
        assert "Tools available to read a URL a skill points at" in instructions
        assert "Host command tools available" not in instructions

    def test_patch_tells_rlm_the_repl_cannot_spawn_processes(self):
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        Skills(SKILLS, mode="code").patch(agent)

        instructions = cast(Any, agent.generate_action.signature).instructions
        assert "cannot spawn processes" in instructions
        assert "`run_skill_script`" in instructions

    def test_patch_uses_the_prefixed_tool_names(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        Skills(SKILLS, name_prefix="sk_").patch(agent)

        instructions = cast(Any, agent.react.signature).instructions
        assert "`sk_activate_skill`" in instructions
        assert "`activate_skill`" not in instructions

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


class TestSandboxInternals:
    def test_family_normalization(self):
        assert _family("python3.13") == "python"
        assert _family("python.exe") == "python"
        assert _family("bash") == "bash"

    def test_sandbox_exec_is_unavailable_off_darwin(self, monkeypatch):
        monkeypatch.setattr(sandbox_module.sys, "platform", "linux")

        assert sandbox_module._available("sandbox-exec") is False

    def test_explicit_backend_that_is_installed(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: True)

        assert resolve_backend("bwrap") == "bwrap"

    def test_required_picks_the_first_installed_backend(self, monkeypatch):
        monkeypatch.setattr(
            sandbox_module, "_available", lambda backend: backend == "firejail"
        )

        assert resolve_backend("required") == "firejail"

    def test_auto_falls_back_to_process_confinement(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        assert resolve_backend("auto") is None

    def test_bwrap_network_flag(self, tmp_path):
        argv = bwrap_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=True,
            allow_write=False,
        )

        assert "--share-net" in argv

    def test_configured_output_truncation(self):
        tools = by_name(Skills(SKILLS, max_output_chars=20))

        output = tools["run_skill_script"](skill="demo-skill", script="loud.py")

        assert "chars omitted" in output

    def test_constructor_rejects_bad_limits(self):
        with pytest.raises(ValueError, match="max_output_chars"):
            ScriptSandbox(max_output_chars=-1)
        with pytest.raises(ValueError, match="interpreters"):
            ScriptSandbox(interpreters=())

    def test_missing_interpreter_on_path(self, tmp_path):
        skill = tmp_path / "s"
        (skill / "scripts").mkdir(parents=True)
        script = skill / "scripts" / "run"
        script.write_text("#!/usr/bin/env golang\nprint(1)\n")
        sandbox = ScriptSandbox(sandbox="off", interpreters=("golang",))

        with pytest.raises(FileNotFoundError, match="Interpreter not found"):
            sandbox.run(script, [], skill_dir=skill)

    def test_shebang_edge_cases(self, tmp_path):
        assert ScriptSandbox._shebang(tmp_path / "missing") == (None, None)

        empty = tmp_path / "empty"
        empty.write_text("")
        assert ScriptSandbox._shebang(empty) == (None, None)

        bare = tmp_path / "bare"
        bare.write_text("#!\n")
        assert ScriptSandbox._shebang(bare) == (None, None)

        flags = tmp_path / "flags"
        flags.write_text("#!/usr/bin/env -i\n")
        assert ScriptSandbox._shebang(flags) == (None, None)

    def test_wrap_uses_the_selected_backend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: True)

        assert (
            ScriptSandbox(sandbox="bwrap")._wrap(["python", "x"], tmp_path, None)[0]
            == "bwrap"
        )
        assert (
            ScriptSandbox(sandbox="firejail")._wrap(["python", "x"], tmp_path, None)[0]
            == "firejail"
        )

    def test_missing_script_file_raises(self, tmp_path):
        skill = copy_skill(tmp_path)

        with pytest.raises(FileNotFoundError, match="Script not found"):
            ScriptSandbox(sandbox="off").run(
                skill / "scripts" / "missing.py", [], skill_dir=skill
            )

    def test_kill_race_is_tolerated(self, monkeypatch, tmp_path):
        skill = copy_skill(tmp_path)
        (skill / "scripts" / "nap.py").write_text("import time; time.sleep(0.3)\n")
        sandbox = ScriptSandbox(sandbox="off", timeout=0.05)

        def gone(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(sandbox_module.os, "killpg", gone)

        result = sandbox.run(skill / "scripts" / "nap.py", [], skill_dir=skill)

        assert result.timed_out is True


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

        monkeypatch.setattr(skills_module, "_skill_dirs", boom)

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


class TestCatalogAndCodeMode:
    def test_catalog_budget_omits_skills(self):
        tools = Skills(SKILLS, max_catalog_chars=10)

        assert "more skills omitted" in tools.catalog()

    def test_catalog_declares_allowed_tools(self):
        tools = Skills(SKILLS)

        assert (
            "<allowed-tools>Bash(git:*) Bash(jq:*)</allowed-tools>" in tools.catalog()
        )

    def test_instructions_drop_script_guidance_when_disabled(self):
        text = Skills(SKILLS, scripts=False).instructions()

        assert "script running is disabled" in text
        assert "run_skill_script" not in text

    def test_instructions_name_the_script_tool(self):
        text = Skills(SKILLS).instructions()

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
            Skills(SKILLS).patch(object())

    def test_unsupported_tool_containers_are_rejected(self):
        with pytest.raises(TypeError, match="Unsupported ReAct tool container"):
            _agent_tools(SimpleNamespace(tools=None), "ReAct")

    def test_negative_limits_are_rejected(self):
        with pytest.raises(ValueError, match="max_resource_chars"):
            Skills(SKILLS, max_resource_chars=-1)
        with pytest.raises(ValueError, match="max_catalog_chars"):
            Skills(SKILLS, max_catalog_chars=-1)
