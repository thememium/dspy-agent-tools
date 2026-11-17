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
    "CLI",
    "DESTRUCTIVE_PATTERNS",
    "FILE_TOOLS",
    "MCP",
    "NETWORK_TOOLS",
    "SECRET_PATTERNS",
    "SHELL_TOOLS",
    "WRITE_TOOLS",
    "Argument",
    "Browser",
    "BrowserSession",
    "BrowserTools",
    "CLITools",
    "Command",
    "MCPTools",
    "Memory",
    "MemoryTools",
    "Offloader",
    "Option",
    "Patch",
    "Policy",
    "PolicyLike",
    "Session",
    "Skill",
    "SkillTools",
    "Skills",
    "Toolbox",
    "Wire",
    "allow_tools",
    "as_policy",
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
    "browser",
    "browser_close",
    "browser_cookies",
    "browser_evaluate",
    "browser_network",
    "browser_open",
    "browser_pdf",
    "browser_record_start",
    "browser_record_stop",
    "browser_restore_tabs",
    "browser_save_tabs",
    "browser_screenshot",
    "browser_snapshot",
    "browser_tabs",
    "browser_wait_response",
    "check",
    "delete_file",
    "deny_code",
    "deny_patterns",
    "deny_shell",
    "deny_tools",
    "deny_urls",
    "destructive_shell",
    "edit_file",
    "no_network",
    "protect",
    "protect_secrets",
    "read_file",
    "read_only",
    "require_approval",
    "safe_defaults",
    "web_news",
    "web_search",
    "wrap_tools",
    "workspace",
    "write_file",
}


def test_public_api_imports():
    """Top-level import pulls in every tool module; exports are callable."""
    assert set(dspy_agent_tools.__all__) == EXPECTED_EXPORTS
    for name in EXPECTED_EXPORTS:
        value = getattr(dspy_agent_tools, name)
        if name.endswith(("_PATTERNS", "_TOOLS")):
            assert isinstance(value, (tuple, frozenset)), name
        else:
            assert callable(value), name


def test_distribution_metadata():
    assert importlib.metadata.version("dspy-agent-tools")
    assert importlib.metadata.metadata("dspy-agent-tools")["Summary"]


if __name__ == "__main__":
    test_public_api_imports()
    test_distribution_metadata()
    print("smoke test passed")
    sys.exit(0)
