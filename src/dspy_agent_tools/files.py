import difflib
from hashlib import sha256
from pathlib import Path

from tree_sitter_language_pack import get_parser


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

    Content is returned with 1-based line numbers prefixed
    to each line, matching the line numbers edit_file uses.
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


def edit_file(
    path: str,
    snapshot: str,
    old_text: str | None = None,
    new_text: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    block: str | None = None,
    language: str = "python",
    min_similarity: float = 0.8,
) -> str:
    """
    Edit a file under optimistic concurrency (snapshot) control.

    Exactly one addressing mode is used per call:

    - block="MyClass.method"  Tree-sitter block addressing. The whole
      definition is replaced by new_text. Works for any language in
      tree-sitter-language-pack (pass its name via `language`).
    - start_line, end_line    Inclusive 1-based line/range replacement.
    - old_text                Exact text replacement. old_text must
      match exactly once; zero or multiple matches raise ValueError.

    Text replacement auto-repairs: if old_text matches zero times, the
    most similar block of lines is replaced instead, as long as its
    similarity is at least min_similarity (0.0-1.0). This recovers
    edits whose old text drifted slightly from the real file
    (whitespace, renamed identifiers, trailing punctuation).

    Returns the new snapshot.
    """
    modes = sum(
        (
            old_text is not None,
            start_line is not None or end_line is not None,
            block is not None,
        )
    )
    if modes != 1:
        raise ValueError("Pass exactly one of old_text, start_line/end_line, or block.")

    if block is not None:
        return _edit_block(path, snapshot, block, new_text, language)

    if start_line is not None or end_line is not None:
        if start_line is None or end_line is None:
            raise ValueError("start_line and end_line must be given together.")
        return _edit_lines(path, snapshot, start_line, end_line, new_text)
    if old_text is None:  # unreachable given the mode check; satisfies ty
        raise ValueError("old_text is required for text replacement.")
    return _edit_text(path, snapshot, old_text, new_text, min_similarity)


def _edit_lines(
    path: str, snapshot: str, start_line: int, end_line: int, new_text: str
) -> str:
    """Replace an inclusive 1-based range of lines."""
    edit_path, content = _checked(path, snapshot)

    lines = content.splitlines(keepends=True)

    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError(
            f"Invalid line range {start_line}-{end_line}; file has {len(lines)} lines."
        )

    # Preserve normal line-oriented behavior.
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    lines[start_line - 1 : end_line] = new_text.splitlines(keepends=True)

    new_content = "".join(lines)
    edit_path.write_text(new_content)

    return _hash(new_content)


def _edit_text(
    path: str, snapshot: str, old_text: str, new_text: str, min_similarity: float
) -> str:
    """
    Replace old_text with new_text.

    old_text must match exactly once. Zero matches fall back to fuzzy
    recovery: the most similar window of lines is replaced instead when
    its similarity reaches min_similarity.
    """
    edit_path, content = _checked(path, snapshot)

    occurrences = content.count(old_text)
    if occurrences > 1:
        raise ValueError(
            f"{path}: {old_text!r} matches {occurrences} times; "
            "pass more surrounding context to make it unique."
        )
    if occurrences == 1:
        new_content = content.replace(old_text, new_text, 1)
        edit_path.write_text(new_content)
        return _hash(new_content)

    # Fuzzy recovery: slide a window the size of old_text over the file.
    old_lines = old_text.splitlines()
    window_size = len(old_lines)
    lines = content.splitlines()

    if window_size == 0 or window_size > len(lines):
        raise ValueError(
            f"{path}: no exact match for {old_text!r} and nothing similar to repair from."
        )

    matcher = difflib.SequenceMatcher()
    matcher.set_seq2("\n".join(old_lines))

    best_ratio = 0.0
    best_start = None
    for start in range(len(lines) - window_size + 1):
        matcher.set_seq1("\n".join(lines[start : start + window_size]))
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_start is None or best_ratio < min_similarity:
        closest = f"{best_ratio:.0%}" if best_start is not None else "none"
        raise ValueError(
            f"{path}: no exact match for {old_text!r}; closest candidate is "
            f"{closest} similar (threshold {min_similarity:.0%})."
        )

    # Preserve the matched window's indentation on the replacement.
    first_line = lines[best_start]
    stripped = first_line.lstrip(" \t")
    indent = first_line[: len(first_line) - len(stripped)] if stripped else ""

    lines[best_start : best_start + window_size] = [
        indent + line if line.strip() else line for line in new_text.splitlines()
    ]
    new_content = "\n".join(lines)
    if content.endswith("\n"):
        new_content += "\n"
    edit_path.write_text(new_content)

    return _hash(new_content)


def _node_name(node):
    """Name text of a definition node, or None if it has no name field."""
    name_node = node.child_by_field_name("name")
    return None if name_node is None else name_node.text.decode()


def _node_body(node):
    """Body of a definition node: 'body' field or the largest named child."""
    body = node.child_by_field_name("body")
    if body is not None:
        return body
    if not node.named_children:
        return None
    return max(node.named_children, key=lambda c: c.end_byte - c.start_byte)


def _find_block_node(node, segments: list[str]):
    """Find the first named definition node matching a dotted name path."""
    target = segments[0]

    for child in node.named_children:
        if _node_name(child) == target:
            if len(segments) == 1:
                return child
            body = _node_body(child)
            if body is not None:
                found = _find_block_node(body, segments[1:])
                if found is not None:
                    return found
        else:
            found = _find_block_node(child, segments)
            if found is not None:
                return found

    return None


def _edit_block(
    path: str, snapshot: str, block: str, new_text: str, language: str
) -> str:
    """Replace a whole syntactic block addressed by a dotted name path."""
    edit_path, content = _checked(path, snapshot)

    tree = get_parser(language).parse(content.encode())
    node = _find_block_node(tree.root_node, block.split("."))
    if node is None:
        raise ValueError(f"{path}: no definition matching {block!r}.")

    if not new_text.endswith("\n"):
        new_text += "\n"

    data = content.encode()
    end = node.end_byte
    if data[end : end + 1] == b"\n" and new_text.endswith("\n"):
        # The node span excludes its trailing newline; the replacement
        # brings its own, so consume the original to avoid a blank line.
        end += 1
    new_data = data[: node.start_byte] + new_text.encode() + data[end:]
    new_content = new_data.decode()
    edit_path.write_text(new_content)

    return _hash(new_content)
