from hashlib import sha256
from pathlib import Path


def _hash(content: str) -> str:
    """Short snapshot ID for optimistic concurrency control."""
    return sha256(content.encode()).hexdigest()[:8]


def read_file(path: str) -> tuple[str, str]:
    """
    Read a file and return:

        (content, snapshot)

    Example:
        content, snapshot = read("app.py")
    """
    content = Path(path).read_text()

    return content, _hash(content)


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


def edit_file(
    path: str,
    snapshot: str,
    start_line: int,
    end_line: int,
    replacement: str,
) -> str:
    """
    Replace an inclusive range of lines.

    Line numbers are 1-based.

    Example:
        snapshot = edit(
            "app.py",
            snapshot,
            start_line=10,
            end_line=12,
            replacement="def hello():\\n    print('hello')",
        )

    Returns the new snapshot.
    """
    edit_path = Path(path)
    content = edit_path.read_text()

    # Optimistic concurrency check
    if _hash(content) != snapshot:
        raise RuntimeError(
            f"{path} changed since it was read. Read the file again before editing."
        )

    lines = content.splitlines(keepends=True)

    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError(
            f"Invalid line range {start_line}-{end_line}; file has {len(lines)} lines."
        )

    # Preserve normal line-oriented behavior.
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"

    lines[start_line - 1 : end_line] = replacement.splitlines(keepends=True)

    new_content = "".join(lines)
    edit_path.write_text(new_content)

    return _hash(new_content)
