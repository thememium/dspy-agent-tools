"""Tests for browser screenshots."""

import tempfile
from pathlib import Path

from dspy_agent_tools.browser import screenshot


class TestScreenshot:
    def test_writes_file(self, session, tmp_path):
        target = tmp_path / "shot.png"
        result = session.screenshot(path=str(target), full_page=True, quality=50)
        assert result["status"] == "ok"
        assert result["path"] == str(target)
        assert result["format"] == "png"
        assert result["bytes"] == len(b"fake-image")
        assert target.read_bytes() == b"fake-image"
        assert session._tab.screenshot_calls == [(str(target), 50, True, False)]

    def test_quality_accepts_numeric_string(self, session, tmp_path):
        result = session.screenshot(path=str(tmp_path / "shot.jpg"), quality="42")
        assert result["status"] == "ok"
        assert session._tab.screenshot_calls[-1][1] == 42

    def test_adds_png_suffix(self, session, tmp_path):
        target = tmp_path / "shot"
        result = session.screenshot(path=str(target))
        assert result["path"] == str(target.with_suffix(".png"))

    def test_default_temp_path(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        result = session.screenshot()
        path = Path(result["path"])
        assert path.parent == tmp_path / "dspy-agent-tools"
        assert path.suffix == ".png"
        assert path.exists()

    def test_base64(self, session):
        result = session.screenshot(path="shot.jpg", as_base64=True)
        assert result["base64"] == "ZmFrZQ=="
        assert result["format"] == "jpeg"
        assert session._tab.screenshot_calls == [("shot.jpg", 80, False, True)]

    def test_quality_is_validated(self, session):
        result = session.screenshot(quality=101)
        assert result["status"] == "error"
        assert "quality" in result["error"]

    def test_quality_rejects_text(self, session):
        result = session.screenshot(quality="high")
        assert result["status"] == "error"
        assert "quality" in result["error"]

    def test_reports_browser_error(self, session):
        session._get_tab()
        session._tab.screenshot_error = RuntimeError("no screenshot")
        result = session.screenshot()
        assert result["status"] == "error"
        assert "no screenshot" in result["error"]


class TestScreenshotFormat:
    def test_screenshot_format(self):
        assert screenshot._screenshot_format(None) == "png"
        assert screenshot._screenshot_format("shot.JPG") == "jpeg"
        assert screenshot._screenshot_format("shot.webp") == "webp"
