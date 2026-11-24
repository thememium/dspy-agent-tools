"""Durable execution for DSPy agents and tools.

``Durable`` wraps an existing ``dspy.Module`` (or a plain Python function) in a
DBOS workflow, turns every predictor call and tool call into a checkpointed
step, and exposes the resulting run through :class:`Run`. ``Human`` adds
persisted approvals and questions to either ordinary Python or an agent's tool
list.

DBOS is an optional dependency. Importing :mod:`dspy_agent_tools` does not
import it; the first ``Durable(...)`` (or ``Run.get``) call raises an
``ImportError`` naming the extra to install::

    uv add "dspy-agent-tools[dbos]"
"""

from __future__ import annotations

from .agent import Durable, DurableAgent
from .events import ApprovalRequest, Event, EventType, QuestionRequest
from .exceptions import (
    DuplicateAgentName,
    DurableError,
    DurableExecutionError,
    HumanInputRequired,
    HumanTimeout,
    NotInWorkflow,
)
from .function import DurableFunction
from .human import Human
from .run import Run
from .schedule import Schedule
from .worker import Worker

__all__ = [
    "ApprovalRequest",
    "DuplicateAgentName",
    "Durable",
    "DurableAgent",
    "DurableError",
    "DurableExecutionError",
    "DurableFunction",
    "Event",
    "EventType",
    "Human",
    "HumanInputRequired",
    "HumanTimeout",
    "NotInWorkflow",
    "QuestionRequest",
    "Run",
    "Schedule",
    "Worker",
]
