"""Browser test fixtures shared by the package."""

import subprocess
from pathlib import Path

import pytest

from dspy_agent_tools.browser import BrowserSession, _pydoll, coerce
from dspy_agent_tools.browser import tools as tools_module

from .fakes import FakeChrome


@pytest.fixture
def fake_chrome(monkeypatch):
    _pydoll.require()
    FakeChrome.instances = []
    FakeChrome.start_error = None
    monkeypatch.setattr(_pydoll, "Chrome", FakeChrome)
    return FakeChrome


@pytest.fixture
def session(fake_chrome):
    session = BrowserSession()
    yield session
    BrowserSession.close(session)


@pytest.fixture
def default_session(monkeypatch, fake_chrome):
    session = BrowserSession()
    monkeypatch.setattr(tools_module, "_default_session", session)
    yield session
    BrowserSession.close(session)


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    state = {"returncode": 0, "stderr": "", "stdout": "", "write": True}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        state["concat"] = Path(command[command.index("-i") + 1]).read_text()
        if state["write"]:
            Path(command[-1]).write_bytes(b"fake-video")
        returncode = state["returncode"]
        stdout = state["stdout"]
        stderr = state["stderr"]
        assert isinstance(returncode, int)
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(coerce, "_ffmpeg_binary", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(subprocess, "run", run)
    return state, calls
