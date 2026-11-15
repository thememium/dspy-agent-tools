"""Tests for dspy_agent_tools.mcp.adapter."""

import asyncio
import inspect
from types import SimpleNamespace

import dspy
import pytest
from mcp.types import Tool as MCPTool

from dspy_agent_tools.mcp.adapter import _error_payload, _read_only

from .helpers import adapter_module


class TestErrorPayload:
    @pytest.mark.parametrize(
        "value",
        [
            {"error": "still starting"},
            '{"error": "still starting"}',
            {"ok": False, "condition": "index_updating"},
            ['{"ok": false}'],
        ],
    )
    def test_detects_error_shapes(self, value):
        assert _error_payload(value)

    @pytest.mark.parametrize(
        "value",
        [
            {"results": []},
            '{"results": []}',
            "plain text",
            {"ok": True},
            {"error": ""},
            b"bytes",
            "{not json",
        ],
    )
    def test_ignores_success_shapes(self, value):
        assert not _error_payload(value)


class TestReadOnlyAnnotation:
    def test_reads_both_sdk_spellings(self):
        assert _read_only(
            SimpleNamespace(annotations=SimpleNamespace(read_only_hint=True))
        )
        assert _read_only(
            SimpleNamespace(annotations=SimpleNamespace(readOnlyHint=True))
        )
        assert _read_only(SimpleNamespace(annotations={"readOnlyHint": True}))

    def test_rejects_missing_or_false_annotations(self):
        assert not _read_only(SimpleNamespace(annotations=None))
        assert not _read_only(SimpleNamespace(annotations=SimpleNamespace()))
        assert not _read_only(
            SimpleNamespace(annotations=SimpleNamespace(read_only_hint=False))
        )
        assert not _read_only(SimpleNamespace())


class TestInternals:
    def test_mcp_tools_expose_schema_signatures(self):
        class Session:
            def __init__(self):
                self.arguments = None

            async def call_tool(self, name, arguments=None):
                self.arguments = arguments
                return SimpleNamespace(content=[], is_error=False)

        advertised = MCPTool(
            name="search",
            description="Search things.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                    "note": {"type": "string"},
                },
                "required": ["query"],
            },
        )
        session = Session()
        base = dspy.Tool.from_mcp_tool(session, advertised)
        tool = adapter_module._as_sync_capable(
            base,
            adapter_module._mcp_signature(
                advertised, base.args or {}, base.arg_types or {}
            ),
        )

        signature = inspect.signature(tool.func)
        assert list(signature.parameters) == ["query", "limit", "note"]
        assert signature.parameters["query"].default is inspect.Parameter.empty
        assert signature.parameters["limit"].default == 10
        assert signature.parameters["note"].default is None
        parameters = tool.format_as_litellm_function_call()["function"]["parameters"]
        assert parameters["required"] == ["query"]

        assert tool(query="dspy", limit=None, note=None) == []
        assert session.arguments == {"query": "dspy"}
        assert asyncio.run(tool.acall(query="rlm", limit=None, note=None)) == []
        assert session.arguments == {"query": "rlm"}

    @pytest.mark.parametrize(
        "schema",
        [
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            {"type": ["string", "null"]},
        ],
    )
    def test_nullable_mcp_argument_reaches_server(self, schema):
        seen = []

        class Session:
            async def call_tool(self, name, arguments=None):
                seen.append(arguments)
                return SimpleNamespace(content=[], is_error=False)

        session = Session()
        advertised = MCPTool(
            name="clear",
            input_schema={
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
            },
        )
        base = dspy.Tool.from_mcp_tool(
            session, adapter_module._dspy_compatible(advertised)
        )
        assert advertised.input_schema["properties"]["value"] == schema
        tool = adapter_module._as_sync_capable(
            base,
            adapter_module._mcp_signature(
                advertised, base.args or {}, base.arg_types or {}
            ),
        )
        assert tool(value=None) == []
        assert asyncio.run(tool.acall(value=None)) == []
        assert seen == [{"value": None}, {"value": None}]

    def test_non_python_parameter_names_use_kwargs(self):
        advertised = MCPTool(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {
                    "class": {"type": "string"},
                    "repo-name": {"type": "string"},
                    "kwargs": {"type": "integer"},
                },
                "required": ["class", "repo-name"],
            },
        )
        signature = adapter_module._mcp_signature(
            advertised, advertised.input_schema["properties"], {}
        )
        bound = signature.bind(**{"class": "A", "repo-name": "org/repo", "kwargs": 1})
        assert bound.arguments["_kwargs"] == {"class": "A", "repo-name": "org/repo"}

        class Session:
            async def call_tool(self, name, arguments=None):
                return SimpleNamespace(content=[], is_error=False)

        base = dspy.Tool.from_mcp_tool(
            Session(), adapter_module._dspy_compatible(advertised)
        )
        tool = adapter_module._as_sync_capable(base, signature)
        assert tool(**{"class": "A", "repo-name": "org/repo", "kwargs": 1}) == []
        assert tool.format_as_litellm_function_call()["function"]["parameters"][
            "required"
        ] == ["class", "repo-name"]

    def test_mcp_signature_tolerates_alternate_schemas(self):
        signature = adapter_module._mcp_signature(
            SimpleNamespace(inputSchema={}),
            {"query": {"type": "string"}},
            {"query": str},
        )
        assert list(signature.parameters) == ["query"]

        assert (
            adapter_module._mcp_signature(
                SimpleNamespace(input_schema="not a schema"), {}, {}
            )
            == inspect.Signature()
        )

        flagged = adapter_module._mcp_signature(
            SimpleNamespace(input_schema={"properties": {"flag": True}}),
            {"flag": {"type": "boolean"}},
            {"flag": bool},
        )
        assert flagged.parameters["flag"].default is None
