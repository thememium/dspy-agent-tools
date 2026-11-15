"""Argument coercion and path/URL normalization."""

from __future__ import annotations

import importlib
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from . import config, errors


def _coerce_timeout(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise errors._ToolError("timeout must be a number of seconds") from exc
    if seconds <= 0:
        raise errors._ToolError("timeout must be greater than zero")
    return min(seconds, config._MAX_TIMEOUT_S)


def _coerce_seconds(value: str | None, default: float) -> float:
    """Coerce an already validated seconds value; ``_wait`` checks the shape."""
    if value is None or value == "":
        return default
    seconds = float(value)
    if seconds < 0:
        raise errors._ToolError("wait seconds cannot be negative")
    return min(seconds, config._MAX_WAIT_S)


def _coerce_quality(value: int | str) -> int:
    try:
        quality = int(value)
    except (TypeError, ValueError) as exc:
        raise errors._ToolError("quality must be an integer from 0 to 100") from exc
    if not 0 <= quality <= 100:
        raise errors._ToolError("quality must be an integer from 0 to 100")
    return quality


def _coerce_fps(value: float | str) -> float:
    try:
        fps = float(value)
    except (TypeError, ValueError) as exc:
        raise errors._ToolError("fps must be a number from 1 to 60") from exc
    if not 1 <= fps <= config._MAX_RECORDING_FPS:
        raise errors._ToolError("fps must be a number from 1 to 60")
    return fps


def _optional_dimension(value: int | str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        dimension = int(value)
    except (TypeError, ValueError) as exc:
        raise errors._ToolError(f"{name} must be a positive integer") from exc
    if dimension <= 0:
        raise errors._ToolError(f"{name} must be a positive integer")
    return dimension


def _video_path(path: str | None) -> Path:
    if path:
        target = Path(path).expanduser()
        if target.is_dir():
            raise errors._ToolError("video path must be a file, not a directory")
        suffix = target.suffix.lower()
        if not suffix:
            target = target.with_suffix(".mp4")
        elif suffix not in config._VIDEO_SUFFIXES:
            raise errors._ToolError("video path must end with .mp4 or .webm")
        else:
            target = target.with_suffix(suffix)
        return target.absolute()
    directory = Path(tempfile.gettempdir()) / "dspy-agent-tools"
    return directory / f"browser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.mp4"


def _ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is not None:
        return binary
    try:
        imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
    except ImportError as exc:
        raise errors._ToolError(
            "video recording requires ffmpeg on PATH or the optional extra: "
            "uv add 'dspy-agent-tools[ffmpeg]'"
        ) from exc
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise errors._ToolError(
            f"could not locate an ffmpeg binary: {errors._format_error(exc)}"
        ) from exc


def _text_arg(value: config.TextArg | None) -> str | None:
    """Tool arguments arrive as JSON; a number where text is expected becomes its text."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _normalize_url(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise errors._ToolError("navigate requires value=URL")
    url = value.strip()
    if url.startswith("//"):
        return "https:" + url
    if config._SCHEME_RE.match(url) or url.startswith(
        ("about:", "data:", "file:", "mailto:", "chrome:", "view-source:")
    ):
        return url
    if url.startswith(("localhost", "127.", "0.0.0.0", "[::1]")):
        return "http://" + url
    return "https://" + url
