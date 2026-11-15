"""Eager and progressive-discovery views over any tool catalog.

``Toolbox`` collects MCP tools, regular ``dspy.Tool`` objects, and plain sync
or async Python functions into one list. With the default ``mode=None`` it
returns every tool eagerly. With a discovery mode it exposes three tools
instead of every definition:

- ``search_tools`` ranks the catalog locally with BM25.
- ``get_tool_details`` returns one tool's description and input schema.
- ``call_tool`` runs one tool by name.

``mode="search"`` returns text results for ``dspy.ReAct`` / ``dspy.ReActV2``;
``mode="code"`` returns structured results for code written by ``dspy.RLM``.
"""

from __future__ import annotations

from .toolbox import Toolbox

__all__ = ["Toolbox"]
