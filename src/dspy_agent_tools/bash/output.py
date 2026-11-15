"""Post-process rg, fd, and grep output into paths, counts, and filters."""

import fnmatch
import re
from pathlib import Path


def _has_hidden_component(line: str) -> bool:
    """True when the path at the start of a grep output line is hidden."""
    head = line.split(":", 1)[0]
    return any(
        part.startswith(".") for part in head.split("/") if part not in ("", ".")
    )


def _sum_counts(lines: list[str]) -> int:
    """Total a "path:count" per-file listing, as rg -c / grep -c print it."""
    total = 0
    for line in lines:
        tail = line.rpartition(":")[2]
        if tail.isdigit():
            total += int(tail)
    return total


def _glob_matches(path: Path, pattern: str) -> bool:
    """Match as rgapi does: a glob with "/" is anchored, else any depth."""
    if "/" in pattern:
        return fnmatch.fnmatchcase(path.as_posix(), pattern)
    return fnmatch.fnmatchcase(path.name, pattern)


def _found_paths(
    lines: list[str], root: Path | str | None, pattern: str | None, kwargs: dict
) -> list[Path]:
    """Filter find output the way rgapi.fd would have matched it."""
    regex = None
    if pattern is not None:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid pattern: {exc}") from exc
    include = kwargs.get("include") or []
    exclude = kwargs.get("exclude") or []
    ext = kwargs.get("ext")
    base = Path.cwd() if root is None else Path(root)
    found: list[Path] = []
    for line in lines:
        rel = line.removeprefix("./")
        if rel in ("", "."):
            continue
        path = Path(rel)
        if not kwargs.get("hidden") and any(p.startswith(".") for p in path.parts):
            continue
        if ext is not None and path.suffix != f".{ext}":
            continue
        if include and not any(_glob_matches(path, g) for g in include):
            continue
        if any(_glob_matches(path, g) for g in exclude):
            continue
        if regex is not None and not regex.search(path.name):
            continue
        found.append(base / path)
    return found
