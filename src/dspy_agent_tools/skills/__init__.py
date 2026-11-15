"""Expose an Agent Skills folder as ``dspy.Tool`` objects.

A skill is a directory with a ``SKILL.md`` file: YAML frontmatter carrying
``name`` and ``description``, followed by Markdown instructions. Skills may
bundle ``scripts/``, ``references/``, and ``assets/`` directories. The format
is the open [Agent Skills](https://agentskills.io/specification) standard, so
skills written for Claude Code, Codex, and other clients work unchanged.

``Skills`` works like :func:`dspy_agent_tools.CLI` and
:func:`dspy_agent_tools.MCP`: point it at the folders that house the skills and
it returns a :class:`SkillTools` list you can pass to any DSPy agent.

```python
import dspy
from dspy_agent_tools import Skills, bash_exec

skills = Skills("~/.agents/skills", ".agents/skills")

agent = dspy.ReAct("question -> answer", tools=skills + [bash_exec])
agent = dspy.RLM("question -> answer", tools=Skills("./skills", mode="code"))
```

The list holds four progressive-disclosure tools:

- ``list_skills`` — names, descriptions, and bundled scripts (~100 tokens per
  skill; the ``SKILL.md`` body is never loaded here).
- ``activate_skill`` — the full ``SKILL.md`` body for one skill.
- ``read_skill_resource`` — one file from ``references/`` or ``assets/``.
- ``run_skill_script`` — one script from ``scripts/``, run in the sandbox
  described in :mod:`dspy_agent_tools.skills.sandbox`.

Pass the list to :func:`dspy_agent_tools.Patch` on an already-built agent to
append the tools and inject the skill catalog into its instructions, together
with any other tool family in the same call; the agent class decides how the
inner predictor is rebuilt, so it works for ``dspy.ReAct``, ``dspy.ReActV2``,
and ``dspy.RLM``.

``mode="search"`` (default) returns text for ReAct-style agents;
``mode="code"`` returns dictionaries for ``dspy.RLM``.
"""

from __future__ import annotations

from .skill import Skill
from .tools import Skills, SkillTools

__all__ = ["Skill", "SkillTools", "Skills"]
