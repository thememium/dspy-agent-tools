"""Tests for the tool-family guide sections."""

from typing import Any, cast

import dspy

from dspy_agent_tools import (
    Patch,
    Skills,
    Toolbox,
    bash_exec,
    delete_file,
    read_file,
    web_search,
    write_file,
)

from .helpers import QA, SKILLS, react_instructions


class TestSections:
    def test_one_section_per_family_present(self):
        agent = dspy.ReAct(QA, tools=[bash_exec], max_iters=2)

        Patch(agent, tools=[read_file, web_search])

        text = react_instructions(agent)
        assert "## Shell" in text
        assert "## Files" in text
        assert "## Web" in text
        assert "## Browser" not in text
        assert "## MCP" not in text

    def test_heading_lists_the_tool_names_it_covers(self):
        agent = dspy.ReAct(QA, tools=[bash_exec])

        Patch(agent, tools=[read_file, write_file, delete_file])

        assert "## Files — `delete_file`, `read_file`, `write_file`" in (
            react_instructions(agent)
        )

    def test_guide_text_comes_first(self):
        agent = dspy.ReAct(QA, tools=[bash_exec])

        Patch(agent, tools=read_file, guide="Files under vendor/ are read-only.")

        text = react_instructions(agent)
        assert text.index("Files under vendor/ are read-only.") < text.index("## Shell")

    def test_describe_false_adds_tools_without_sections(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=read_file, describe=False)

        assert "read_file" in cast(Any, agent.tools)
        assert "## Files" not in react_instructions(agent)

    def test_no_family_sections_for_unknown_tools(self):
        def ping() -> str:
            """Reply with pong."""
            return "pong"

        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=ping)

        assert "ping" in cast(Any, agent.tools)
        assert "##" not in react_instructions(agent)

    def test_toolbox_facade_gets_its_own_section(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=Toolbox([bash_exec, read_file], mode="search"))

        text = react_instructions(agent)
        assert "## On-demand tools" in text
        assert "## Shell" not in text

    def test_foreign_shell_names_get_the_shell_section(self):
        def shell(command: str) -> str:
            """Run a command."""
            return ""

        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=shell)

        assert "## Shell — `shell`" in react_instructions(agent)

    def test_families_that_hide_their_names_get_a_bare_heading(self):
        def call_tool(name: str) -> str:
            """Call a tool on the server."""
            return ""

        call_tool.__module__ = "dspy_agent_tools.mcp"
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=call_tool)

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

        Patch(agent, tools=[make(index) for index in range(10)])

        assert "+2 more" in react_instructions(agent)

    def test_skills_source_replaces_the_template(self):
        agent = dspy.ReAct(QA, tools=[])

        Patch(agent, tools=Skills(SKILLS))

        text = react_instructions(agent)
        assert "<available_skills>" in text
        assert "A skill is a folder of instructions and reference files" not in text
