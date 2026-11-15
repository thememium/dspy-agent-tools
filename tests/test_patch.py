"""Tests for dspy_agent_tools.patch and its tool-family guide."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, get_args

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import (
    Skills,
    Toolbox,
    bash_exec,
    delete_file,
    patch,
    read_file,
    web_search,
    write_file,
)

SKILLS = Path(__file__).parent / "fixtures" / "skills"


class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


def react_instructions(agent: Any) -> str:
    return agent.react.signature.instructions


def rlm_instructions(agent: Any) -> str:
    return agent.generate_action.signature.instructions


class TestSections:
    def test_one_section_per_family_present(self):
        agent = dspy.ReAct(QA, tools=[bash_exec], max_iters=2)

        patch(agent, read_file, web_search)

        text = react_instructions(agent)
        assert "## Shell" in text
        assert "## Files" in text
        assert "## Web" in text
        assert "## Browser" not in text
        assert "## MCP" not in text

    def test_heading_lists_the_tool_names_it_covers(self):
        agent = dspy.ReAct(QA, tools=[bash_exec])

        patch(agent, [read_file, write_file, delete_file])

        assert "## Files — `delete_file`, `read_file`, `write_file`" in (
            react_instructions(agent)
        )

    def test_guide_text_comes_first(self):
        agent = dspy.ReAct(QA, tools=[bash_exec])

        patch(agent, read_file, guide="Files under vendor/ are read-only.")

        text = react_instructions(agent)
        assert text.index("Files under vendor/ are read-only.") < text.index("## Shell")

    def test_describe_false_adds_tools_without_sections(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, read_file, describe=False)

        assert "read_file" in cast(Any, agent.tools)
        assert "## Files" not in react_instructions(agent)

    def test_no_family_sections_for_unknown_tools(self):
        def ping() -> str:
            """Reply with pong."""
            return "pong"

        agent = dspy.ReAct(QA, tools=[])

        patch(agent, ping)

        assert "ping" in cast(Any, agent.tools)
        assert "##" not in react_instructions(agent)

    def test_toolbox_facade_gets_its_own_section(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, Toolbox([bash_exec, read_file], mode="search"))

        text = react_instructions(agent)
        assert "## On-demand tools" in text
        assert "## Shell" not in text

    def test_foreign_shell_names_get_the_shell_section(self):
        def shell(command: str) -> str:
            """Run a command."""
            return ""

        agent = dspy.ReAct(QA, tools=[])

        patch(agent, shell)

        assert "## Shell — `shell`" in react_instructions(agent)

    def test_families_that_hide_their_names_get_a_bare_heading(self):
        def call_tool(name: str) -> str:
            """Call a tool on the server."""
            return ""

        call_tool.__module__ = "dspy_agent_tools.mcp"
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, call_tool)

        text = react_instructions(agent)
        assert "## MCP\n\n" in text
        assert "## MCP —" not in text

    def test_long_families_summarise_the_remaining_names(self):
        def make(index: int):
            def command() -> str:
                """Run a command."""
                return ""

            command.__name__ = f"bash_tool_{index}"
            command.__module__ = "dspy_agent_tools.bash"
            return command

        agent = dspy.ReAct(QA, tools=[])

        patch(agent, [make(index) for index in range(10)])

        assert "+2 more" in react_instructions(agent)

    def test_skills_source_replaces_the_template(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, Skills(SKILLS))

        text = react_instructions(agent)
        assert "<available_skills>" in text
        assert "A skill is a folder of instructions and reference files" not in text


class TestModules:
    def test_patches_react_in_place(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=3)

        returned = patch(agent, read_file)

        assert returned is agent
        assert set(cast(Any, agent.tools)) == {"read_file", "finish"}
        assert agent.max_iters == 3
        choices = get_args(
            agent.react.signature.output_fields["next_tool_name"].annotation
        )
        assert "read_file" in choices

    def test_patches_react_v2(self):
        agent = dspy.ReActV2(QA, tools=[])

        patch(agent, read_file)

        assert "read_file" in cast(Any, agent.tools)
        assert "## Files" in react_instructions(agent)

    def test_patches_rlm(self):
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        patch(agent, read_file)

        assert "read_file" in cast(Any, agent._user_tools)
        assert "## Files" in rlm_instructions(agent)

    def test_rlm_is_told_the_repl_cannot_spawn_processes(self):
        agent = dspy.RLM(QA, tools=[])

        patch(agent, read_file)

        assert "cannot spawn processes" in rlm_instructions(agent)

    def test_react_is_not_told_about_a_repl(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, read_file)

        assert "REPL" not in react_instructions(agent)

    def test_module_state_is_preserved(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=4)
        agent.callbacks = ["marker"]

        patch(agent, read_file)

        assert agent.callbacks == ["marker"]
        assert agent.max_iters == 4

    def test_unsupported_modules_are_rejected(self):
        with pytest.raises(TypeError, match="supports dspy.ReAct"):
            patch(dspy.Predict(QA), read_file)

    def test_missing_signature_is_rejected(self):
        with pytest.raises(TypeError, match="signature"):
            patch(object(), read_file)

    def test_string_sources_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[])

        with pytest.raises(TypeError, match="sources are tools"):
            patch(agent, "read_file")

    def test_non_iterable_sources_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[])

        with pytest.raises(TypeError, match="sources are tools"):
            patch(agent, object())

    def test_describe_false_still_keeps_the_guide(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, read_file, guide="Stay in the repo.", describe=False)

        text = react_instructions(agent)
        assert "Stay in the repo." in text
        assert "## Files" not in text


class TestComposition:
    def test_patching_twice_keeps_both_families(self):
        agent = dspy.ReAct(QA, tools=[])

        patch(agent, read_file)
        patch(agent, bash_exec)

        text = react_instructions(agent)
        assert "## Files" in text
        assert "## Shell" in text
        assert text.count("## Files") == 1

    def test_repeated_patch_is_a_no_op(self):
        agent = dspy.ReAct(QA, tools=[])
        patch(agent, read_file, bash_exec, guide="Stay in the repo.")
        before = dict(cast(Any, agent.tools))
        text = react_instructions(agent)

        patch(agent, read_file, bash_exec, guide="Stay in the repo.")

        assert dict(cast(Any, agent.tools)) == before
        assert react_instructions(agent) == text

    def test_patching_an_agent_that_already_has_the_tools_adds_guidance(self):
        agent = dspy.ReAct(QA, tools=[read_file])

        patch(agent, read_file)

        assert sum(name == "read_file" for name in cast(Any, agent.tools)) == 1
        assert "## Files" in react_instructions(agent)

    def test_foreign_tool_names_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[dspy.Tool(lambda: "x", name="read_file")])

        with pytest.raises(ValueError, match="different tool named 'read_file'"):
            patch(agent, read_file)


class TestBehavior:
    def test_patched_loop_can_call_the_new_tool(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("patched in")

        agent = dspy.ReAct(QA, tools=[], max_iters=3)
        patch(agent, read_file)
        lm = DummyLM(
            [
                {
                    "next_thought": "read the file",
                    "next_tool_name": "read_file",
                    "next_tool_args": {"path": str(target)},
                },
                {
                    "next_thought": "done",
                    "next_tool_name": "finish",
                    "next_tool_args": {},
                },
                {"reasoning": "read it", "answer": "patched in"},
            ]
        )

        with dspy.context(lm=lm):
            prediction = agent(question="what is in notes.txt?")

        content, _snapshot = prediction.trajectory["observation_0"]
        assert "patched in" in content
        assert prediction.answer == "patched in"

    def test_skills_patch_is_the_generic_patch(self):
        agent = dspy.ReAct(QA, tools=[])
        skills = Skills(SKILLS)

        skills.patch(agent)
        patched_with = react_instructions(agent)

        other = dspy.ReAct(QA, tools=[])
        patch(other, skills)

        assert react_instructions(other) == patched_with
