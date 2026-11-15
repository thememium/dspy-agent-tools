"""Tests for browser value coercion helpers."""

import importlib
import shutil
from types import SimpleNamespace

import pytest

from dspy_agent_tools.browser import coerce, errors


class TestFfmpegBinary:
    def test_ffmpeg_binary(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        assert coerce._ffmpeg_binary() == "/usr/bin/ffmpeg"

    def test_ffmpeg_binary_falls_back_to_imageio(self, monkeypatch):
        module = SimpleNamespace(get_ffmpeg_exe=lambda: "/bundle/ffmpeg")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", lambda _name: module)
        assert coerce._ffmpeg_binary() == "/bundle/ffmpeg"

    def test_ffmpeg_binary_reports_bundled_binary_error(self, monkeypatch):
        def unavailable():
            raise RuntimeError("no bundled binary")

        module = SimpleNamespace(get_ffmpeg_exe=unavailable)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", lambda _name: module)
        with pytest.raises(errors._ToolError, match="no bundled binary"):
            coerce._ffmpeg_binary()
