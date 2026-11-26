"""File I/O operations with optimistic concurrency control."""

import itertools
import re
from hashlib import sha256
from pathlib import Path

# read_file's display prefix: a 1-based line number, right-aligned, then " | ".
_NUMBER_PREFIX = re.compile(r"^\s*(\d+) \| ")


def _hash(content: str) -> str:
    """Short snapshot ID for optimistic concurrency control."""
    return sha256(content.encode()).hexdigest()[:8]


def _unnumber(text: str) -> str:
    """Strip read_file's " N | " display prefixes from pasted content.

    Content counts as read_file output when every non-empty line carries
    the prefix, or when at least two lines do, they make up at least half
    the non-empty lines, and each run of adjacent numbered lines counts
    upward one by one. Anything else is left alone, so real content that
    merely starts with a number and a pipe (a table row, say) survives.
    """
    lines = text.splitlines(keepends=True)
    numbered = [
        (index, int(match.group(1)))
        for index, line in enumerate(lines)
        if (match := _NUMBER_PREFIX.match(line))
    ]
    if not numbered:
        return text
    non_empty = sum(1 for line in lines if line.strip())
    sequential = all(
        later == earlier + 1
        for (first, earlier), (second, later) in itertools.pairwise(numbered)
        if second == first + 1
    )
    if not (
        len(numbered) == non_empty
        or (len(numbered) >= 2 and len(numbered) * 2 >= non_empty and sequential)
    ):
        return text
    return "".join(_NUMBER_PREFIX.sub("", line) for line in lines)


def _checked(path: str, snapshot: str) -> tuple[Path, str]:
    """Read a file and enforce the snapshot (optimistic concurrency)."""
    edit_path = Path(path)
    content = edit_path.read_text()

    if _hash(content) != snapshot:
        raise RuntimeError(
            f"{path} changed since it was read. Read the file again before editing."
        )

    return edit_path, content


def read_file(path: str) -> tuple[str, str]:
    """
    Read a file and return:

        (numbered_content, snapshot)

    Content is returned with 1-based line numbers prefixed to each
    line. The " N | " prefix is display-only: quote text without it
    when calling edit_file or write_file.
    """
    content = Path(path).read_text()
    snapshot = _hash(content)

    numbered = "\n".join(
        f"{i:>5} | {line}" for i, line in enumerate(content.splitlines(), 1)
    )

    return numbered, snapshot


def write_file(path: str, content: str, snapshot: str | None = None) -> str:
    """
    Write an entire file.

    If snapshot is supplied, reject the write if the file changed
    since it was read. read_file's " N | " display prefixes are stripped
    from content before writing, so pasted numbered output cannot end up
    in the file.

    Returns the new snapshot.
    """
    write_path = Path(path)

    if snapshot is not None:
        try:
            current = write_path.read_text()
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"{path} changed since it was read (file is missing). "
                "Read the file again before writing."
            ) from exc

        if _hash(current) != snapshot:
            raise RuntimeError(
                f"{path} changed since it was read. Read the file again before writing."
            )

    content = _unnumber(content)
    write_path.write_text(content)

    return _hash(content)


def delete_file(path: str, snapshot: str | None = None) -> str:
    """
    Delete a file.

    If snapshot is supplied, reject the deletion if the file changed
    since it was read.

    Only regular files and symlinks are removed; directories raise
    ValueError, and a missing path raises FileNotFoundError.

    Returns a confirmation message.
    """
    delete_path = Path(path)

    if not (delete_path.exists() or delete_path.is_symlink()):
        raise FileNotFoundError(f"{path}: no such file to delete.")

    if delete_path.is_dir() and not delete_path.is_symlink():
        raise ValueError(f"{path} is a directory; delete_file only removes files.")

    if snapshot is not None:
        _checked(path, snapshot)

    delete_path.unlink()

    return f"Deleted {path}"
