"""Tests for dspy_agent_tools.skills.tools and its Patch integration."""

from __future__ import annotations

from typing import Any, cast, get_args

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import Patch, Skills, SkillTools, bash_exec, web_search

from .helpers import QA, SKILLS, by_name


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

        Patch(agent, tools=Skills(SKILLS, mode="code", name_prefix="sk_"))

        assert "sk_activate_skill" in cast(Any, agent._user_tools)

    def test_rejects_unknown_mode_and_empty_roots(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            Skills(SKILLS, mode=cast(Any, "tools"))
        with pytest.raises(ValueError, match="at least one skill folder"):
            Skills()


class TestPatch:
    def test_patches_react_in_place(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        returned = Patch(agent, tools=tools)

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

        Patch(agent, tools=tools)

        assert "run_skill_script" in cast(Any, agent.tools)
        assert "<available_skills>" in cast(Any, agent.react.signature).instructions

    def test_patches_rlm(self):
        tools = Skills(SKILLS)
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        Patch(agent, tools=tools)

        assert "activate_skill" in cast(Any, agent._user_tools)
        instructions = cast(Any, agent.generate_action.signature).instructions
        assert "<available_skills>" in instructions

    def test_patch_is_idempotent(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=tools)
        Patch(agent, tools=tools)
        before = dict(cast(Any, agent.tools))
        Patch(agent, tools=tools)

        assert dict(cast(Any, agent.tools)) == before
        instructions = cast(Any, agent.react.signature).instructions
        assert instructions.count("<available_skills>") == 1

    def test_patch_only_adds_catalog_when_tools_are_already_present(self):
        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=tools)

        Patch(agent, tools=tools)

        assert sum(name == "list_skills" for name in cast(Any, agent.tools)) == 1
        instructions = cast(Any, agent.react.signature).instructions
        assert "<available_skills>" in instructions

    def test_patch_rejects_foreign_tool_names(self):
        tools = Skills(SKILLS)
        foreign = dspy.Tool(lambda: "x", name="list_skills", desc="not ours")
        agent = dspy.ReAct(QA, tools=[foreign])

        with pytest.raises(ValueError, match="different tool named 'list_skills'"):
            Patch(agent, tools=tools)

    def test_patch_rejects_a_second_catalog(self):
        first = Skills(SKILLS)
        second = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=first)
        with pytest.raises(ValueError, match="different tool named .list_skills."):
            Patch(agent, tools=second)

    def test_patch_rejects_unsupported_modules(self):
        agent = dspy.Predict("question -> answer")

        with pytest.raises(TypeError, match="supports dspy.ReAct"):
            Patch(agent, tools=Skills(SKILLS))

    def test_patch_forbids_inventing_command_output(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        Patch(agent, tools=Skills(SKILLS))

        instructions = cast(Any, agent.react.signature).instructions
        assert "never claim you ran a command" in instructions
        assert "never invent its output" in instructions
        assert "Host command tools available" not in instructions

    def test_patch_names_the_shell_tools_it_has(self):
        agent = dspy.ReAct(QA, tools=[bash_exec], max_iters=2)

        Patch(agent, tools=Skills(SKILLS))

        instructions = cast(Any, agent.react.signature).instructions
        assert "Host command tools available to you: `bash_exec`." in instructions

    def test_patch_names_fetch_tools_without_a_shell(self):
        agent = dspy.ReAct(QA, tools=[web_search], max_iters=2)

        Patch(agent, tools=Skills(SKILLS))

        instructions = cast(Any, agent.react.signature).instructions
        assert "Tools available to read a URL a skill points at" in instructions
        assert "Host command tools available" not in instructions

    def test_patch_tells_rlm_the_repl_cannot_spawn_processes(self):
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        Patch(agent, tools=Skills(SKILLS, mode="code"))

        instructions = cast(Any, agent.generate_action.signature).instructions
        assert "cannot spawn processes" in instructions
        assert "`run_skill_script`" in instructions

    def test_patch_uses_the_prefixed_tool_names(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=2)

        Patch(agent, tools=Skills(SKILLS, name_prefix="sk_"))

        instructions = cast(Any, agent.react.signature).instructions
        assert "`sk_activate_skill`" in instructions
        assert "`activate_skill`" not in instructions

    def test_patched_react_loop_can_use_a_skill(self):
        class QA(dspy.Signature):
            question: str = dspy.InputField()
            answer: str = dspy.OutputField()

        tools = Skills(SKILLS)
        agent = dspy.ReAct(QA, tools=[], max_iters=3)
        Patch(agent, tools=tools)
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
