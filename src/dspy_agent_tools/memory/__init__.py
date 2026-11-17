"""Long-term memory as a tool family over any duck-typed store.

The store contract is deliberately narrow and backend-agnostic: the
``dspy_lancedb_memory`` package satisfies it, and a test double needs three
methods.
"""

from .tools import MAX_CONTENT, MAX_RECORDS, Memory, MemoryTools

__all__ = ["MAX_CONTENT", "MAX_RECORDS", "Memory", "MemoryTools"]
