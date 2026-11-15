"""Shared helpers for the MCP tests."""

import importlib
import sys
from pathlib import Path

from dspy_agent_tools import MCP

SERVER = Path(__file__).parents[1] / "fixtures" / "mcp_server.py"
RETRY_SERVER = Path(__file__).parents[1] / "fixtures" / "retry_server.py"

adapter_module = importlib.import_module("dspy_agent_tools.mcp.adapter")
tools_module = importlib.import_module("dspy_agent_tools.mcp.tools")


def start(**kwargs):
    return MCP(command=sys.executable, args=[str(SERVER)], **kwargs)


def start_retry(**kwargs):
    return MCP(command=sys.executable, args=[str(RETRY_SERVER)], **kwargs)
