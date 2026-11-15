"""Contract tests for dspy_agent_tools.utils.dspy."""

import asyncio
import sys

import pytest
from dspy import Tool

from dspy_agent_tools.utils.dspy import load_tool, sync_tool_class


def test_load_tool_returns_dspy_tool():
    assert load_tool("unavailable") is Tool


def test_load_tool_raises_with_message_when_import_blocked(monkeypatch):
    monkeypatch.setitem(sys.modules, "dspy", None)
    with pytest.raises(ImportError, match="custom import failure"):
        load_tool("custom import failure")


def test_sync_tool_class_caches_per_drop_nulls():
    plain = sync_tool_class(drop_nulls=False)
    plain_again = sync_tool_class()
    dropping = sync_tool_class(drop_nulls=True)

    assert plain is plain_again
    assert dropping is not plain
    assert sync_tool_class(drop_nulls=True) is dropping


def test_sync_tool_class_runs_async_function_through_sync_path():
    calls = []

    async def record(value):
        calls.append(value)
        return "recorded"

    tool = sync_tool_class()(record, name="record", desc="record a value")

    assert tool(value="x") == "recorded"
    assert calls == ["x"]


def test_sync_tool_class_acall_awaits_async_function():
    calls = []

    async def record(value):
        calls.append(value)
        return "recorded"

    tool = sync_tool_class()(record, name="record", desc="record a value")

    assert asyncio.run(tool.acall(value="x")) == "recorded"
    assert calls == ["x"]


def test_sync_tool_class_drop_nulls_removes_none_kwargs():
    calls = []

    async def record(value="DEFAULT", other=0):
        calls.append({"value": value, "other": other})
        return "recorded"

    dropping = sync_tool_class(drop_nulls=True)(record, name="drop", desc="drop")
    keeping = sync_tool_class(drop_nulls=False)(record, name="keep", desc="keep")

    assert dropping(value=None, other=2) == "recorded"
    assert keeping(value=None, other=2) == "recorded"
    assert calls == [
        {"value": "DEFAULT", "other": 2},
        {"value": None, "other": 2},
    ]
