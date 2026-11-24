"""DBOS serializer for DSPy values.

DBOS's default serializer is ``pickle``. DSPy's ``Prediction`` objects hold
dynamically generated signatures and exception classes that plain pickle
cannot resolve by name. ``cloudpickle`` (already a DSPy dependency) serializes
those by value, so durable results and recorded failures round-trip inside the
same application.

The serializer is intentionally permissive: plain pickle data remains readable
because cloudpickle emits the standard pickle protocol.
"""

from __future__ import annotations

import base64
import pickle
from typing import Any


def _dumps(value: Any) -> bytes:
    try:
        import cloudpickle
    except ImportError:  # pragma: no cover - DSPy installs cloudpickle
        return pickle.dumps(value)
    return cloudpickle.dumps(value)


def _loads(data: bytes) -> Any:
    try:
        import cloudpickle
    except ImportError:  # pragma: no cover - DSPy installs cloudpickle
        return pickle.loads(data)
    return cloudpickle.loads(data)


class CloudpickleSerializer:
    """``dbos.Serializer`` implementation used by the durable runtime."""

    def serialize(self, data: Any) -> str:
        return base64.b64encode(_dumps(data)).decode("ascii")

    def deserialize(self, serialized_data: str) -> Any:
        return _loads(base64.b64decode(serialized_data))

    def name(self) -> str:
        return "dspy-cloudpickle"
