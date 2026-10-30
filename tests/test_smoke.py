"""Smoke tests for the built distribution.

Run by the publish workflow against the artifacts in dist/:
    uv run --isolated --no-project --with dist/*.whl tests/test_smoke.py
    uv run --isolated --no-project --with dist/*.tar.gz tests/test_smoke.py

The package under test comes from the installed artifact (--with), not the
local checkout. Also runnable standalone or via pytest.
"""

import importlib.metadata
import sys

import dspy_agent_tools

EXPECTED_EXPORTS = {
    "MCPTools",
    "bash_curl",
    "bash_exec",
    "bash_fetch",
    "bash_find",
    "bash_find_batch",
    "bash_glob",
    "bash_glob_batch",
    "bash_ls",
    "bash_ripgrep",
    "bash_ripgrep_batch",
    "edit_file",
    "mcp",
    "read_file",
    "web_news",
    "web_search",
    "write_file",
}


def test_public_api_imports():
    """Top-level import pulls in every tool module; exports are callable."""
    assert set(dspy_agent_tools.__all__) == EXPECTED_EXPORTS
    for name in EXPECTED_EXPORTS:
        assert callable(getattr(dspy_agent_tools, name)), name


def test_distribution_metadata():
    assert importlib.metadata.version("dspy-agent-tools")
    assert importlib.metadata.metadata("dspy-agent-tools")["Summary"]


if __name__ == "__main__":
    test_public_api_imports()
    test_distribution_metadata()
    print("smoke test passed")
    sys.exit(0)
