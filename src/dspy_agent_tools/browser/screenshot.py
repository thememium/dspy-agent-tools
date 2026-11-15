"""Page screenshots."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import coerce, errors, session


def _screenshot_path(path: str | None) -> Path:
    if path:
        target = Path(path).expanduser()
        return (
            target.with_suffix(target.suffix.lower())
            if target.suffix
            else target.with_suffix(".png")
        )
    directory = Path(tempfile.gettempdir()) / "dspy-agent-tools"
    return directory / f"browser-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.png"


def _screenshot_format(path: str | None) -> str:
    if path:
        suffix = Path(path).expanduser().suffix.lstrip(".").lower()
        if suffix:
            return "jpeg" if suffix == "jpg" else suffix
    return "png"


def _screenshot_tab(
    tab: Any,
    path: str | None,
    full_page: bool,
    quality: int | str,
    as_base64: bool,
) -> dict:
    quality = coerce._coerce_quality(quality)
    if as_base64:
        format_path = _screenshot_path(path) if path else Path("screenshot.png")
        data = tab.take_screenshot(
            path=str(format_path),
            quality=quality,
            beyond_viewport=full_page,
            as_base64=True,
        )
        return {"format": _screenshot_format(str(format_path)), "base64": data}
    target = _screenshot_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tab.take_screenshot(
        path=str(target),
        quality=quality,
        beyond_viewport=full_page,
    )
    return {
        "path": str(target),
        "format": _screenshot_format(str(target)),
        "bytes": target.stat().st_size,
    }


def _screenshot(
    session: session.BrowserSession,
    path: str | None,
    full_page: bool,
    quality: int | str,
    as_base64: bool,
) -> dict:
    try:
        data = session._run(
            lambda tab: _screenshot_tab(tab, path, full_page, quality, as_base64)
        )
    except Exception as exc:  # noqa: BLE001 - returned as tool status
        return errors._error("screenshot", exc)
    return {"status": "ok", "action": "screenshot", **data}
