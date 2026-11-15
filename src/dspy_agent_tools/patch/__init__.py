"""Add tools and tool guidance to an agent that was already built.

``Patch`` adds tool sources to a built agent — tool lists such as ``MCP(...)``,
``CLI(...)``, ``Browser()``, ``Skills(...)``, ``Toolbox(...)``, or bare
``dspy.Tool`` objects and functions — appends them to an existing agent, and
recomputes a short policy section per tool family in the agent's
instructions.

```python
import dspy
from dspy_agent_tools import CLI, Skills, bash_exec, Patch, read_file

agent = dspy.ReAct("question -> answer", tools=[bash_exec])
Patch(agent, tools=[read_file, Skills("./skills"), CLI("gh")])
```

The guidance goes into the signature's instructions, which DSPy sends as the
system message — never into new input fields, which would change what every
caller has to pass (``dspy.RLM`` refuses a call whose inputs were not
supplied, and ``dspy.ReAct`` copies them into the predictor it builds).

Sections come from the tool families present on the agent, so the block names
what the agent can actually do. A source that knows more than its family
template — ``Skills``, which carries the ``<available_skills>`` catalog — can
define ``family`` and a ``section(tools)`` method; that method is called with
the agent's full tool list and its output replaces the template.
"""

from __future__ import annotations

from .patch import Patch

__all__ = ["Patch"]
