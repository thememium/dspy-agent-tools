"""CDP screencast recording and ffmpeg encoding."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import (
    _pydoll,
    coerce,
    config,
    errors,
    session,
)


class _VideoRecorder:
    """Collect CDP screencast frames and encode them with ffmpeg."""

    def __init__(
        self,
        path: Path,
        ffmpeg: str,
        fps: float,
        quality: int,
        max_width: int | None,
        max_height: int | None,
        show_cursor: bool = False,
    ) -> None:
        self.path = path
        self.ffmpeg = ffmpeg
        self.fps = fps
        self.quality = quality
        self.max_width = max_width
        self.max_height = max_height
        self.show_cursor = show_cursor
        self.cursor_script_id: str | None = None
        self.cursor_token = uuid4().hex
        self.frames_dir = Path(tempfile.mkdtemp(prefix="dspy-agent-tools-video-"))
        self.frames: list[tuple[float, Path]] = []
        self.lock = threading.Lock()
        self.tab: Any | None = None
        self.callback_id: int | None = None
        self._last_kept_timestamp: float | None = None
        self._frame_index = 0

    def on_frame(self, event: dict) -> None:
        """Store one CDP frame; runs on pydoll's callback thread."""
        params = event.get("params") or {}
        session_id = params.get("sessionId")
        metadata = params.get("metadata") or {}
        try:
            raw_timestamp = metadata.get("timestamp")
            timestamp = time.time() if raw_timestamp is None else float(raw_timestamp)
            if (
                self._last_kept_timestamp is not None
                and abs(timestamp - self._last_kept_timestamp) < 1.0 / self.fps
            ):
                return
            data = params.get("data")
            if not isinstance(data, str):
                raise errors._ToolError("screencast frame did not contain image data")
            raw = base64.b64decode(data, validate=True)
            with self.lock:
                self._frame_index += 1
                frame_path = self.frames_dir / f"frame-{self._frame_index:06d}.jpg"
                frame_path.write_bytes(raw)
                self.frames.append((timestamp, frame_path))
                self._last_kept_timestamp = timestamp
        except Exception as exc:  # noqa: BLE001 - one bad frame must not end the recording
            config.logger.warning(
                "Could not store a screencast frame: %s", errors._format_error(exc)
            )
        finally:
            self._ack(session_id)

    def _ack(self, session_id: Any) -> None:
        if session_id is None or self.tab is None:
            return
        try:
            self.tab.execute_command(
                _pydoll.PageCommands.screencast_frame_ack(session_id)
            )
        except Exception as exc:  # noqa: BLE001 - acknowledgement is best-effort
            config.logger.warning(
                "Could not acknowledge a screencast frame: %s",
                errors._format_error(exc),
            )


def _start_screencast(tab: Any, recorder: _VideoRecorder) -> None:
    tab.enable_page_events()
    recorder.tab = tab
    recorder.callback_id = tab.on(_pydoll.PageEvent.SCREENCAST_FRAME, recorder.on_frame)
    try:
        if recorder.show_cursor:
            response = tab.execute_command(
                {
                    "method": "Page.addScriptToEvaluateOnNewDocument",
                    "params": {
                        "source": _cursor_script(recorder.cursor_token),
                        "worldName": "dspy-recording-cursor",
                        "runImmediately": True,
                    },
                }
            )
            recorder.cursor_script_id = response["result"]["identifier"]
        tab.execute_command(
            _pydoll.PageCommands.start_screencast(
                _pydoll.ScreencastFormat.JPEG,
                quality=recorder.quality,
                max_width=recorder.max_width,
                max_height=recorder.max_height,
                every_nth_frame=1,
            )
        )
    except Exception:
        _remove_cursor(tab, recorder)
        recorder.callback_id = _remove_callback(tab, recorder.callback_id)
        raise


def _stop_screencast(tab: Any, recorder: _VideoRecorder) -> None:
    try:
        tab.execute_command(_pydoll.PageCommands.stop_screencast())
    except Exception as exc:  # noqa: BLE001 - captured frames are still usable
        config.logger.warning(
            "Could not stop the screencast: %s", errors._format_error(exc)
        )
    finally:
        _remove_cursor(tab, recorder)
        recorder.callback_id = _remove_callback(tab, recorder.callback_id)


def _cursor_script(token: str) -> str:
    """Draw the pointer in each document without affecting hit testing."""
    return r"""
(() => {
  const token = TOKEN;
  let cursor, timer;
  const mount = () => {
    if (cursor || !document.documentElement) return;
    cursor = document.createElement('div');
    cursor.setAttribute('data-dspy-recording-cursor', '');
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;z-index:2147483647;pointer-events:none;display:none;background:#fff;border:2px solid #111;border-radius:50%;box-sizing:border-box;box-shadow:0 1px 4px #0009';
    document.documentElement.appendChild(cursor);
  };
  const move = e => {
    mount();
    if (!cursor) return;
    cursor.style.display = 'block';
    cursor.style.transform = `translate(${e.clientX - 9}px, ${e.clientY - 9}px)`;
  };
  const down = e => {
    move(e);
    if (!cursor) return;
    cursor.style.background = '#ff5252';
    clearTimeout(timer);
    timer = setTimeout(() => { if (cursor) cursor.style.background = '#fff'; }, 350);
  };
  const leave = () => { if (cursor) cursor.style.display = 'none'; };
  const cleanup = e => {
    if (e.data !== token) return;
    for (const frame of document.querySelectorAll('iframe,frame')) {
      try { frame.contentWindow.postMessage(token, '*'); } catch (_) {}
    }
    removeEventListener('mousemove', move, true);
    removeEventListener('mousedown', down, true);
    removeEventListener('mouseout', out, true);
    removeEventListener('message', cleanup, true);
    clearTimeout(timer);
    if (cursor) cursor.remove();
  };
  const out = e => { if (!e.relatedTarget) leave(); };
  addEventListener('mousemove', move, true);
  addEventListener('mousedown', down, true);
  addEventListener('mouseout', out, true);
  addEventListener('message', cleanup, true);
})();
""".replace("TOKEN", json.dumps(token))


def _remove_cursor(tab: Any, recorder: _VideoRecorder) -> None:
    if recorder.cursor_script_id is None:
        return
    try:
        tab.execute_command(
            {
                "method": "Page.removeScriptToEvaluateOnNewDocument",
                "params": {"identifier": recorder.cursor_script_id},
            }
        )
        tab.execute_command(
            {
                "method": "Runtime.evaluate",
                "params": {
                    "expression": f"window.postMessage({json.dumps(recorder.cursor_token)}, '*')"
                },
            }
        )
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        config.logger.warning(
            "Could not remove recording cursor: %s", errors._format_error(exc)
        )
    finally:
        recorder.cursor_script_id = None


def _remove_callback(tab: Any, callback_id: int | None) -> None:
    if callback_id is None:
        return
    try:
        tab.remove_callback(callback_id)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        config.logger.warning(
            "Could not remove the screencast callback: %s", errors._format_error(exc)
        )


def _discard_recording(recorder: _VideoRecorder) -> None:
    if recorder.tab is not None:
        _remove_cursor(recorder.tab, recorder)
        _remove_callback(recorder.tab, recorder.callback_id)
    recorder.callback_id = None
    shutil.rmtree(recorder.frames_dir, ignore_errors=True)


def _frame_durations(frames: list[tuple[float, Path]], fps: float) -> list[float]:
    durations: list[float] = []
    for index, (timestamp, _path) in enumerate(frames):
        if index + 1 < len(frames):
            duration = frames[index + 1][0] - timestamp
        else:
            duration = 1.0 / fps
        durations.append(max(0.001, duration))
    return durations


def _write_concat(
    recorder: _VideoRecorder,
    frames: list[tuple[float, Path]],
    durations: list[float],
) -> Path:
    concat = recorder.frames_dir / "frames.txt"
    lines = ["ffconcat version 1.0"]
    for (_timestamp, frame_path), duration in zip(frames, durations, strict=True):
        lines.append(f"file '{frame_path.as_posix()}'")
        lines.append(f"duration {duration:.6f}")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat


def _ffmpeg_command(recorder: _VideoRecorder, concat: Path, output: Path) -> list[str]:
    command = [
        recorder.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat),
        "-fps_mode",
        "vfr",
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-an",
    ]
    if output.suffix.lower() == ".webm":
        command.extend(
            ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-pix_fmt", "yuv420p"]
        )
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
    command.extend(["-y", str(output)])
    return command


def _finalize_recording(recorder: _VideoRecorder) -> dict[str, Any]:
    output = recorder.path.with_name(f"{recorder.path.stem}.part{recorder.path.suffix}")
    try:
        with recorder.lock:
            frames = list(recorder.frames)
        if not frames:
            raise errors._ToolError("no video frames were captured")
        durations = _frame_durations(frames, recorder.fps)
        concat = _write_concat(recorder, frames, durations)
        recorder.path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            _ffmpeg_command(recorder, concat, output),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise errors._ToolError(
                f"ffmpeg could not encode the video: {detail[-500:] or 'unknown error'}"
            )
        if not output.exists():
            raise errors._ToolError("ffmpeg did not write the video file")
        output.replace(recorder.path)
        return {
            "path": str(recorder.path),
            "format": recorder.path.suffix.lstrip(".").lower(),
            "bytes": recorder.path.stat().st_size,
            "frames": len(frames),
            "duration_s": round(sum(durations), 3),
        }
    finally:
        output.unlink(missing_ok=True)
        shutil.rmtree(recorder.frames_dir, ignore_errors=True)


def _record_start(
    session: session.BrowserSession,
    path: str | None,
    fps: float | str,
    quality: int | str,
    max_width: int | str | None,
    max_height: int | str | None,
    show_cursor: bool = False,
) -> dict:
    try:
        if not isinstance(show_cursor, bool):
            raise errors._ToolError("show_cursor must be a boolean")
        fps_value = coerce._coerce_fps(fps)
        quality_value = coerce._coerce_quality(quality)
        width = coerce._optional_dimension(max_width, "max_width")
        height = coerce._optional_dimension(max_height, "max_height")
        recorder = _VideoRecorder(
            coerce._video_path(path),
            coerce._ffmpeg_binary(),
            fps_value,
            quality_value,
            width,
            height,
            show_cursor,
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("record_start", exc)
    try:
        session._start_recording(recorder)
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        _discard_recording(recorder)
        return errors._error("record_start", exc)
    return {
        "status": "ok",
        "action": "record_start",
        "path": str(recorder.path),
        "fps": fps_value,
        "quality": quality_value,
        "max_width": width,
        "max_height": height,
        "show_cursor": show_cursor,
    }


def _record_stop(session: session.BrowserSession) -> dict:
    try:
        data = session._stop_recording()
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("record_stop", exc)
    return {"status": "ok", "action": "record_stop", **data}
