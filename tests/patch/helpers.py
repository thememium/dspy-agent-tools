"""Shared signatures and helpers for the patch tests."""

from pathlib import Path
from typing import Any

import dspy

SKILLS = Path(__file__).parents[1] / "fixtures" / "skills"


class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()


def react_instructions(agent: Any) -> str:
    return agent.react.signature.instructions


def rlm_instructions(agent: Any) -> str:
    return agent.generate_action.signature.instructions
