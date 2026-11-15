"""File I/O operations with optimistic concurrency control."""

from hashlib import sha256
from pathlib import Path


def _hash(content: str) -> str:
    """Short snapshot ID for optimistic concurrency control."""
    return sha256(content.encode()).hexdigest()[:8]


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
    since it was read.

    Returns the new snapshot.
    """
    write_path = Path(path)

    if snapshot is not None and write_path.exists():
        current = write_path.read_text()

        if _hash(current) != snapshot:
            raise RuntimeError(
                f"{path} changed since it was read. Read the file again before writing."
            )

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
