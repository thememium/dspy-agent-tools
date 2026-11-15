"""Tests for dspy_agent_tools.patch."""

from __future__ import annotations

from typing import Any, cast, get_args

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from dspy_agent_tools import Patch, Skills, bash_exec, read_file, write_file

from .helpers import QA, SKILLS, react_instructions, rlm_instructions


class TestModules:
    def test_patches_react_in_place(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=3)

        returned = Patch(agent, tools=read_file)

        assert returned is agent
        assert set(cast(Any, agent.tools)) == {"read_file", "finish"}
        assert agent.max_iters == 3
        choices = get_args(
            agent.react.signature.output_fields["next_tool_name"].annotation
        )
        assert "read_file" in choices

    def test_patches_react_v2(self):
        agent = dspy.ReActV2(QA, tools=[])

        Patch(agent, tools=read_file)

        assert "read_file" in cast(Any, agent.tools)
        assert "## Files" in react_instructions(agent)

    def test_patches_rlm(self):
        agent = dspy.RLM(QA, tools=[], max_iters=2)

        Patch(agent, tools=read_file)

        assert "read_file" in cast(Any, agent._user_tools)
        assert "## Files" in rlm_instructions(agent)

    def test_rlm_is_told_the_repl_cannot_spawn_processes(self):
        agent = dspy.RLM(QA, tools=[])

        Patch(agent, tools=read_file)

        assert "cannot spawn processes" in rlm_instructions(agent)

    def test_react_is_not_told_about_a_repl(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file)

        assert "REPL" not in react_instructions(agent)

    def test_module_state_is_preserved(self):
        agent = dspy.ReAct(QA, tools=[], max_iters=4)
        agent.callbacks = ["marker"]

        Patch(agent, tools=read_file)

        assert agent.callbacks == ["marker"]
        assert agent.max_iters == 4

    def test_unsupported_modules_are_rejected(self):
        with pytest.raises(TypeError, match="supports dspy.ReAct"):
            Patch(dspy.Predict(QA), tools=read_file)

    def test_missing_signature_is_rejected(self):
        with pytest.raises(TypeError, match="signature"):
            Patch(object(), tools=read_file)

    def test_string_sources_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[])

        with pytest.raises(TypeError, match="takes tools"):
            Patch(agent, tools="read_file")

    def test_non_iterable_sources_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[])

        with pytest.raises(TypeError, match="takes tools"):
            Patch(agent, tools=object())

    def test_describe_false_still_keeps_the_guide(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file, guide="Stay in the repo.", describe=False)

        text = react_instructions(agent)
        assert "Stay in the repo." in text
        assert "## Files" not in text


class TestComposition:
    def test_patching_twice_keeps_both_families(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file)
        Patch(agent, tools=bash_exec)

        text = react_instructions(agent)
        assert "## Files" in text
        assert "## Shell" in text
        assert text.count("## Files") == 1

    def test_repeated_patch_is_a_no_op(self):
        agent = dspy.ReAct(QA, tools=[])
        Patch(agent, tools=[read_file, bash_exec], guide="Stay in the repo.")
        before = dict(cast(Any, agent.tools))
        text = react_instructions(agent)

        Patch(agent, tools=[read_file, bash_exec], guide="Stay in the repo.")

        assert dict(cast(Any, agent.tools)) == before
        assert react_instructions(agent) == text

    def test_patching_an_agent_that_already_has_the_tools_adds_guidance(self):
        agent = dspy.ReAct(QA, tools=[read_file])

        Patch(agent, tools=read_file)

        assert sum(name == "read_file" for name in cast(Any, agent.tools)) == 1
        assert "## Files" in react_instructions(agent)

    def test_foreign_tool_names_are_rejected(self):
        agent = dspy.ReAct(QA, tools=[dspy.Tool(lambda: "x", name="read_file")])

        with pytest.raises(ValueError, match="different tool named 'read_file'"):
            Patch(agent, tools=read_file)

    def test_conflicting_incoming_tool_names_rejected_before_mutation(self):
        agent = dspy.ReAct(QA, tools=[])
        first = dspy.Tool(lambda: "first", name="duplicate")
        second = dspy.Tool(lambda: "second", name="duplicate")
        before = dict(cast(Any, agent.tools))
        with pytest.raises(ValueError, match="different tool named 'duplicate'"):
            Patch(agent, tools=[first, second])
        assert agent.tools == before


class TestBehavior:
    @pytest.mark.parametrize("module", [dspy.ReAct, dspy.ReActV2, dspy.RLM])
    def test_patch_preserves_predictor_lms_and_configuration(self, module):
        agent = module(QA, tools=[])
        lm = DummyLM([])
        agent.set_lm(lm)
        for _, predictor in agent.named_predictors():
            predictor.config["temperature"] = 0.25
        Patch(agent, tools=read_file)
        assert agent.predictors()
        for predictor in agent.predictors():
            assert predictor.lm is lm
            assert predictor.config["temperature"] == 0.25

    def test_patched_loop_can_call_the_new_tool(self, tmp_path):
        target = tmp_path / "notes.txt"
        target.write_text("patched in")

        agent = dspy.ReAct(QA, tools=[], max_iters=3)
        Patch(agent, tools=read_file)
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

    def test_skills_source_injects_the_catalog_once(self):
        agent = dspy.ReAct(QA, tools=[])
        skills = Skills(SKILLS)

        Patch(agent, tools=skills)
        Patch(agent, tools=skills)

        text = react_instructions(agent)
        assert text.count("<available_skills>") == 1
        assert text.count("<!-- dspy-agent-tools:begin -->") == 1


class TestDrop:
    def test_drop_revokes_a_tool_and_its_section(self):
        agent = dspy.ReAct(QA, tools=[bash_exec, read_file])

        Patch(agent, drop="bash_exec")

        assert set(cast(Any, agent.tools)) == {"read_file", "finish"}
        text = react_instructions(agent)
        assert "## Shell" not in text
        assert "## Files" in text

    def test_drop_takes_a_tool_or_a_list(self):
        agent = dspy.ReAct(QA, tools=[bash_exec, read_file, write_file])

        Patch(agent, drop=[read_file, "write_file"])

        assert set(cast(Any, agent.tools)) == {"bash_exec", "finish"}

    def test_unknown_drop_names_warn_and_change_nothing(self, caplog):
        agent = dspy.ReAct(QA, tools=[bash_exec])
        Patch(agent)
        before = react_instructions(agent)

        with caplog.at_level("WARNING", logger="dspy_agent_tools.patch"):
            Patch(agent, drop="bash_exce")

        assert "no tool named bash_exce" in caplog.text
        assert set(cast(Any, agent.tools)) == {"bash_exec", "finish"}
        assert react_instructions(agent) == before

    def test_dropping_the_same_tool_twice_is_harmless(self, caplog):
        agent = dspy.ReAct(QA, tools=[bash_exec, read_file])

        Patch(agent, drop="bash_exec")
        after_first = react_instructions(agent)
        with caplog.at_level("WARNING", logger="dspy_agent_tools.patch"):
            Patch(agent, drop="bash_exec")

        assert react_instructions(agent) == after_first

    def test_add_and_drop_the_same_tool_is_rejected(self):
        agent = dspy.ReAct(QA, tools=[])

        with pytest.raises(ValueError, match="in both tools= and drop="):
            Patch(agent, tools=read_file, drop="read_file")

    def test_internal_tools_cannot_be_dropped(self):
        agent = dspy.ReAct(QA, tools=[read_file])

        with pytest.raises(ValueError, match="cannot be dropped"):
            Patch(agent, drop="finish")


class TestManagedRegion:
    def test_edited_guide_replaces_the_old_copy(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file, guide="Never commit.")
        Patch(agent, tools=read_file, guide="Never push.")

        text = react_instructions(agent)
        assert "Never push." in text
        assert "Never commit." not in text
        assert text.count("<!-- dspy-agent-tools:begin -->") == 1

    def test_guide_is_kept_when_a_later_call_omits_it(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file, guide="Never commit.")
        Patch(agent, tools=bash_exec)

        text = react_instructions(agent)
        assert "Never commit." in text
        assert "## Files" in text
        assert "## Shell" in text

    def test_empty_guide_clears_it(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file, guide="Never commit.")
        Patch(agent, guide="")

        text = react_instructions(agent)
        assert "Never commit." not in text
        assert "## Files" in text

    def test_hand_written_section_is_left_alone(self):
        signature = QA.with_instructions("## Shell\n\nOnly run the shell for tests.")
        agent = dspy.ReAct(signature, tools=[])

        Patch(agent, tools=bash_exec)

        text = react_instructions(agent)
        assert text.count("## Shell") == 1
        assert "These run host commands" not in text

    def test_compiled_demos_are_reported_before_they_are_dropped(self, caplog):
        agent = dspy.ReAct(QA, tools=[])
        agent.react.demos = [
            dspy.Example(question="q", answer="a").with_inputs("question")
        ]

        with caplog.at_level("WARNING", logger="dspy_agent_tools.patch"):
            Patch(agent, tools=read_file)

        assert "drops 1 few-shot demo" in caplog.text
        assert agent.react.demos == []
