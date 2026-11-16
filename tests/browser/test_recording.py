"""Tests for browser screen recording."""

import importlib
import shutil
from pathlib import Path

import pytest

from dspy_agent_tools.browser import coerce

from .fakes import frame_event, screencast_callback, screencast_callback_id


class TestRecording:
    def test_cursor_install_and_cleanup_on_recorded_tab(
        self, session, fake_ffmpeg, monkeypatch
    ):
        session._get_tab()
        tab = session._tab
        execute = tab.execute_command

        def command(value, timeout=60):
            result = execute(value, timeout=timeout)
            if value["method"] == "Page.addScriptToEvaluateOnNewDocument":
                return {"result": {"identifier": "cursor-script"}}
            return result

        monkeypatch.setattr(tab, "execute_command", command)
        assert session.record_start(show_cursor=True)["show_cursor"] is True
        add = next(
            c
            for c in tab.commands
            if c["method"] == "Page.addScriptToEvaluateOnNewDocument"
        )
        assert add["params"]["runImmediately"] is True
        screencast_callback(tab)(frame_event())
        assert session.record_stop()["status"] == "ok"
        assert any(
            c["method"] == "Page.removeScriptToEvaluateOnNewDocument"
            and c["params"]["identifier"] == "cursor-script"
            for c in tab.commands
        )
        assert any(c["method"] == "Runtime.evaluate" for c in tab.commands)

    def test_cursor_requires_boolean(self, session, fake_ffmpeg):
        assert (
            "show_cursor must be a boolean"
            in session.record_start(show_cursor="false")["error"]
        )

    def test_cursor_cleanup_failure_preserves_recording(
        self, session, fake_ffmpeg, monkeypatch, caplog
    ):
        session._get_tab()
        tab = session._tab
        execute = tab.execute_command

        def command(value, timeout=60):
            if value["method"] == "Page.addScriptToEvaluateOnNewDocument":
                return {"result": {"identifier": "cursor-script"}}
            if value["method"] == "Page.removeScriptToEvaluateOnNewDocument":
                raise RuntimeError("cleanup failed")
            return execute(value, timeout=timeout)

        monkeypatch.setattr(tab, "execute_command", command)
        assert session.record_start(show_cursor=True)["status"] == "ok"
        recorder = session._recorder
        screencast_callback(tab)(frame_event())
        assert session.record_stop()["status"] == "ok"
        assert recorder.cursor_script_id is None
        assert "cleanup failed" in caplog.text

    @pytest.mark.parametrize("stop", ["record_stop", "close"])
    def test_stop_after_switch_targets_recorded_tab(
        self, session, tmp_path, fake_ffmpeg, stop
    ):
        assert session.record_start(path=str(tmp_path / "clip.mp4"))["status"] == "ok"
        original = session._tab
        screencast_callback(original)(frame_event())
        callback_id = screencast_callback_id(original)
        assert session.tabs("new")["status"] == "ok"
        other = session._tab
        assert original is not other
        getattr(session, stop)()
        assert original.commands[-1]["method"] == "Page.stopScreencast"
        assert callback_id in original.removed_callbacks
        assert not any(
            command["method"] == "Page.stopScreencast" for command in other.commands
        )

    def test_start_and_stop_encodes_video(self, session, tmp_path, fake_ffmpeg):
        state, calls = fake_ffmpeg
        result = session.record_start(
            path=str(tmp_path / "demo.MP4"),
            fps=10,
            quality=50,
            max_width=800,
            max_height=600,
        )
        assert result["status"] == "ok"
        assert Path(result["path"]) == tmp_path / "demo.mp4"
        assert result["fps"] == 10.0
        assert result["quality"] == 50
        assert result["max_width"] == 800
        assert result["max_height"] == 600
        tab = session._tab
        assert tab.page_events == 2  # session start plus the screencast
        assert tab.commands[0]["method"] == "Page.startScreencast"
        assert tab.commands[0]["params"] == {
            "format": "jpeg",
            "quality": 50,
            "maxWidth": 800,
            "maxHeight": 600,
            "everyNthFrame": 1,
        }

        callback = screencast_callback(tab)
        callback(frame_event(timestamp=1.0))
        callback(frame_event(b"second", timestamp=1.5))
        recorder = session._recorder
        assert recorder is not None
        assert len(recorder.frames) == 2
        assert all(frame_path.exists() for _stamp, frame_path in recorder.frames)
        assert [command["method"] for command in tab.commands] == [
            "Page.startScreencast",
            "Page.screencastFrameAck",
            "Page.screencastFrameAck",
        ]

        screencast_id = screencast_callback_id(tab)
        stopped = session.record_stop()
        assert stopped["status"] == "ok"
        assert stopped["frames"] == 2
        assert stopped["duration_s"] == 0.6
        assert stopped["format"] == "mp4"
        assert stopped["bytes"] == len(b"fake-video")
        assert Path(stopped["path"]).read_bytes() == b"fake-video"
        assert tab.removed_callbacks == [screencast_id]
        assert tab.commands[-1]["method"] == "Page.stopScreencast"
        assert session._recorder is None

        video_command = calls[0]
        assert "libx264" in video_command
        assert "-movflags" in video_command
        assert Path(video_command[-1]).name == "demo.part.mp4"
        assert "duration 0.500000" in state["concat"]
        assert "duration 0.100000" in state["concat"]

    def test_webm_uses_vp9(self, session, tmp_path, fake_ffmpeg):
        _state, calls = fake_ffmpeg
        assert session.record_start(path=str(tmp_path / "demo.webm"))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        result = session.record_stop()
        assert result["format"] == "webm"
        assert "libvpx-vp9" in calls[0]
        assert "-movflags" not in calls[0]

    def test_default_path_is_temp_mp4(self, session, fake_ffmpeg):
        result = session.record_start()
        assert result["status"] == "ok"
        assert Path(result["path"]).suffix == ".mp4"
        assert "dspy-agent-tools" in result["path"]

    def test_video_path_adds_mp4_suffix(self, tmp_path):
        assert coerce._video_path(str(tmp_path / "clip")).name == "clip.mp4"

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"fps": "fast"}, "fps must"),
            ({"fps": 0}, "fps must"),
            ({"fps": 61}, "fps must"),
            ({"quality": 101}, "quality must"),
            ({"max_width": "wide"}, "max_width must"),
            ({"max_width": 0}, "max_width must"),
            ({"max_height": -1}, "max_height must"),
            ({"path": "demo.mov"}, ".mp4 or .webm"),
        ],
    )
    def test_start_rejects_invalid_options(
        self, session, tmp_path, fake_ffmpeg, kwargs, message
    ):
        if kwargs.get("path") == "demo.mov":
            kwargs["path"] = str(tmp_path / "demo.mov")
        result = session.record_start(**kwargs)
        assert result["status"] == "error"
        assert message in result["error"]

    def test_start_rejects_directory_path(self, session, tmp_path, fake_ffmpeg):
        result = session.record_start(path=str(tmp_path))
        assert result["status"] == "error"
        assert "not a directory" in result["error"]

    def test_start_requires_ffmpeg(self, session, monkeypatch):
        def missing(_name):
            raise ImportError("no imageio")

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(importlib, "import_module", missing)
        result = session.record_start()
        assert result["status"] == "error"
        assert "ffmpeg" in result["error"]
        assert "[ffmpeg]" in result["error"]

    def test_start_twice_is_rejected(self, session, tmp_path, fake_ffmpeg):
        first = session.record_start(path=str(tmp_path / "one.mp4"))
        assert first["status"] == "ok"
        second = session.record_start(path=str(tmp_path / "two.mp4"))
        assert second["status"] == "error"
        assert "already in progress" in second["error"]
        assert session._recorder is not None
        assert session._recorder.path.name == "one.mp4"

    def test_start_failure_removes_callback(self, session, fake_ffmpeg):
        session._get_tab()
        tab = session._tab
        tab.command_error = RuntimeError("no screencast")
        tab.remove_callback_error = RuntimeError("no remove")
        result = session.record_start()
        assert result["status"] == "error"
        assert "no screencast" in result["error"]
        assert session._recorder is None
        assert len(tab.removed_callbacks) == 1

    def test_bad_frame_and_failed_stop_are_reported(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        tab = session._tab
        tab.command_error = RuntimeError("ack failed")
        callback = screencast_callback(tab)
        callback({"params": {"metadata": {"timestamp": 1.0}, "sessionId": 1}})
        callback(
            {
                "params": {
                    "data": "!!!!",
                    "metadata": {"timestamp": 2.0},
                    "sessionId": 1,
                }
            }
        )
        recorder = session._recorder
        assert recorder is not None
        assert recorder.frames == []
        screencast_id = screencast_callback_id(tab)
        result = session.record_stop()
        assert result["status"] == "error"
        assert "no video frames" in result["error"]
        assert tab.removed_callbacks == [screencast_id]

    def test_frame_ack_is_skipped_without_callback_context(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        recorder = session._recorder
        assert recorder is not None
        recorder.tab = None
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=1.0, session_id=1))
        callback(frame_event(timestamp=2.0, session_id=None))
        assert len(recorder.frames) == 2
        assert session.record_stop()["status"] == "ok"

    def test_frame_without_metadata_uses_wall_clock(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=None))
        assert len(session._recorder.frames) == 1
        assert session.record_stop()["status"] == "ok"

    def test_fps_caps_captured_frames(self, session, fake_ffmpeg):
        assert session.record_start(fps=1)["status"] == "ok"
        callback = screencast_callback(session._tab)
        callback(frame_event(timestamp=1.0))
        callback(frame_event(timestamp=1.5))
        assert len(session._recorder.frames) == 1
        assert session.record_stop()["frames"] == 1

    def test_ffmpeg_failure_is_reported(self, session, tmp_path, fake_ffmpeg):
        state, _calls = fake_ffmpeg
        target = tmp_path / "out.mp4"
        assert session.record_start(path=str(target))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        state["returncode"] = 1
        state["stderr"] = "boom"
        state["write"] = False
        result = session.record_stop()
        assert result["status"] == "error"
        assert "boom" in result["error"]
        assert not target.exists()

    def test_ffmpeg_without_output_is_reported(self, session, tmp_path, fake_ffmpeg):
        state, _calls = fake_ffmpeg
        target = tmp_path / "out.mp4"
        assert session.record_start(path=str(target))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        state["write"] = False
        result = session.record_stop()
        assert result["status"] == "error"
        assert "did not write" in result["error"]
        assert not target.exists()

    def test_stop_without_recording(self, session):
        result = session.record_stop()
        assert result["status"] == "error"
        assert "no video recording" in result["error"]
        assert session.take_last_recording() is None

    def test_stop_then_close_does_not_repeat_recording(
        self, session, tmp_path, fake_ffmpeg
    ):
        assert session.record_start(path=str(tmp_path / "once.mp4"))["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        assert session.record_stop()["status"] == "ok"
        assert session.close() is True
        assert session.last_recording is None
        assert session.take_last_recording() is None

    def test_close_encodes_active_recording(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        assert session.close() is True
        recording = session.last_recording
        assert recording is not None
        assert recording["status"] == "ok"
        result = session.take_last_recording()
        assert result == recording
        assert session.take_last_recording() is None
        assert session.close() is False

    def test_close_reports_recording_failure(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        session._tab = None
        assert session.close() is True
        assert session.last_recording is not None
        assert session.last_recording["status"] == "error"

    def test_reset_discards_recording(self, session, fake_ffmpeg):
        assert session.record_start()["status"] == "ok"
        screencast_callback(session._tab)(frame_event())
        recorder = session._recorder
        assert recorder is not None
        frames_dir = recorder.frames_dir
        session._reset()
        assert session._recorder is None
        assert not frames_dir.exists()
        assert session.last_recording is not None
        assert "disconnected" in session.last_recording["error"]
