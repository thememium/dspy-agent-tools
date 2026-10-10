"""Tests for dspy_agent_tools.files."""

import pytest

from dspy_agent_tools import files as files_mod
from dspy_agent_tools.files import (
    _edit_block,
    _edit_lines,
    _edit_text,
    _find_block_node,
    _hash,
    _node_body,
    _node_name,
    _unnumber,
    edit_file,
    read_file,
    write_file,
)

SAMPLE = """\
def alpha():
    return 1


class Canvas:
    def draw(self):
        return "drawn"


def omega():
    return 9
"""


# --- _hash ---------------------------------------------------------------


class TestHash:
    def test_deterministic(self):
        assert _hash("abc") == _hash("abc")

    def test_differs_per_content(self):
        assert _hash("abc") != _hash("abd")

    def test_is_short_hex(self):
        h = _hash("abc")
        assert len(h) == 8
        int(h, 16)


# --- read_file / write_file ---------------------------------------------


class TestReadFile:
    def test_returns_numbered_lines_and_snapshot(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("one\ntwo\n")
        numbered, snapshot = read_file(str(f))
        assert snapshot == _hash("one\ntwo\n")
        lines = numbered.splitlines()
        assert lines[0] == "    1 | one"
        assert lines[1] == "    2 | two"


class TestWriteFile:
    def test_creates_and_returns_snapshot(self, tmp_path):
        f = tmp_path / "f.txt"
        snap = write_file(str(f), "hello\n")
        assert f.read_text() == "hello\n"
        assert snap == _hash("hello\n")

    def test_matching_snapshot_allows_write(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("old\n")
        snap = _hash("old\n")
        write_file(str(f), "new\n", snapshot=snap)
        assert f.read_text() == "new\n"

    def test_stale_snapshot_rejected(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("changed\n")
        stale = _hash("original\n")
        with pytest.raises(RuntimeError, match="changed since it was read"):
            write_file(str(f), "new\n", snapshot=stale)

    def test_snapshot_on_missing_file_skips_check(self, tmp_path):
        f = tmp_path / "f.txt"
        snap = write_file(str(f), "fresh\n", snapshot=_hash("whatever\n"))
        assert f.read_text() == "fresh\n"
        assert snap == _hash("fresh\n")


# --- edit_file mode dispatch --------------------------------------------


class TestEditFileModeDispatch:
    def test_no_mode_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="exactly one"):
            edit_file(str(f), _hash("x"))

    def test_two_modes_raise(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="exactly one"):
            edit_file(str(f), _hash("x"), old_text="x", start_line=1, end_line=1)

    def test_all_three_modes_raise(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="exactly one"):
            edit_file(
                str(f), _hash("x"), old_text="x", start_line=1, end_line=1, block="a"
            )

    def test_start_line_without_end_line_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="together"):
            edit_file(str(f), _hash("x"), start_line=1)

    def test_end_line_without_start_line_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="together"):
            edit_file(str(f), _hash("x"), end_line=1)


# --- old_text replacement ------------------------------------------------


class TestEditTextExact:
    def test_exact_replacement(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        snap = _hash(SAMPLE)
        new_snap = edit_file(str(f), snap, old_text="return 1", new_text="return 42")
        assert "return 42" in f.read_text()
        assert new_snap == _hash(f.read_text())

    def test_multiple_matches_raise(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a b a b")
        with pytest.raises(ValueError, match="matches 2 times"):
            edit_file(str(f), _hash("a b a b"), old_text="a", new_text="z")

    def test_zero_matches_below_threshold_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        with pytest.raises(ValueError, match="no exact match"):
            edit_file(str(f), _hash(SAMPLE), old_text="completely unrelated text")

    def test_stale_snapshot_rejected(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        with pytest.raises(RuntimeError, match="changed since it was read"):
            edit_file(str(f), _hash("stale"), old_text="alpha", new_text="x")


class TestEditTextNumberedPrefix:
    def test_pasted_read_file_output_is_unnumbered(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("alpha\nbeta\n")
        numbered, _ = read_file(str(f))
        # The agent pasted the numbered output verbatim.
        new_snap = edit_file(
            str(f), _hash("alpha\nbeta\n"), old_text=numbered, new_text="gamma"
        )
        assert f.read_text() == "gamma\n"
        assert new_snap == _hash("gamma\n")


class TestUnnumber:
    def test_strips_prefixes_when_all_lines_prefixed(self):
        text = "    1 | alpha\n    2 | beta\n"
        assert _unnumber(text) == "alpha\nbeta"

    def test_keeps_empty_lines(self):
        text = "    1 | alpha\n\n    2 | beta\n"
        assert _unnumber(text) == "alpha\n\nbeta"

    def test_plain_text_untouched(self):
        text = "1 | x\nplain\n"
        assert _unnumber(text) == text

    def test_empty_text_untouched(self):
        assert _unnumber("") == ""


class TestEditTextFuzzy:
    def test_fuzzy_repairs_drifted_text(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        # Old text drifted: "retun" typo and different spacing.
        old = "def alpha():\n    retun 1"
        new_snap = edit_file(
            str(f), _hash(SAMPLE), old_text=old, new_text="def alpha():\n    return 2"
        )
        content = f.read_text()
        assert "return 2" in content
        assert new_snap == _hash(content)

    def test_fuzzy_preserves_indentation(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        old = 'def draw(self):\n    return "drawn"'
        edit_file(
            str(f),
            _hash(SAMPLE),
            old_text=old,
            new_text='def draw(self):\n    return "redrawn"',
        )
        assert '        return "redrawn"' in f.read_text()

    def test_fuzzy_below_threshold_raises_with_ratio(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text(SAMPLE)
        old = "\n".join(f"unrelated {i}" for i in range(4))
        with pytest.raises(ValueError, match="closest candidate is") as exc:
            edit_file(
                str(f), _hash(SAMPLE), old_text=old, new_text="x", min_similarity=0.99
            )
        assert "threshold 99%" in str(exc.value)

    def test_nothing_similar_when_file_too_short(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("tiny\n")
        with pytest.raises(ValueError, match="nothing similar to repair from"):
            edit_file(
                str(f), _hash("tiny\n"), old_text="line1\nline2\nline3", new_text="x"
            )

    def test_fuzzy_replacement_can_shrink_window(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("keep\none\ntwo\nthree\nkeep2\n")
        old = "one\ntwo\nthree"
        edit_file(str(f), _hash(f.read_text()), old_text=old, new_text="single")
        assert f.read_text() == "keep\nsingle\nkeep2\n"

    def test_fuzzy_empty_new_text_lines_keep_blank(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\nd\n")
        # Replacement lines that are blank stay blank (no indent added).
        old = "b\nc"
        edit_file(str(f), _hash(f.read_text()), old_text=old, new_text="\n")
        assert f.read_text() == "a\n\n\nd\n"


# --- line replacement ----------------------------------------------------


class TestEditLines:
    def test_replace_range(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\n")
        edit_file(str(f), _hash("a\nb\nc\n"), start_line=2, end_line=2, new_text="B")
        assert f.read_text() == "a\nB\nc\n"

    def test_replace_multi_line_range(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\nd\n")
        edit_file(
            str(f), _hash(f.read_text()), start_line=2, end_line=3, new_text="X\nY"
        )
        assert f.read_text() == "a\nX\nY\nd\n"

    def test_new_text_gets_trailing_newline(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\n")
        edit_file(str(f), _hash("a\nb\nc\n"), start_line=2, end_line=2, new_text="B")
        assert f.read_text().endswith("B\nc\n")

    def test_empty_new_text_deletes_lines(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\nc\n")
        edit_file(str(f), _hash("a\nb\nc\n"), start_line=2, end_line=2, new_text="")
        assert f.read_text() == "a\nc\n"

    def test_start_below_one_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\n")
        with pytest.raises(ValueError, match="Invalid line range"):
            edit_file(str(f), _hash("a\n"), start_line=0, end_line=1)

    def test_end_before_start_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\n")
        with pytest.raises(ValueError, match="Invalid line range"):
            edit_file(str(f), _hash("a\nb\n"), start_line=2, end_line=1)

    def test_end_beyond_file_raises(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\n")
        with pytest.raises(ValueError, match="file has 1 lines"):
            edit_file(str(f), _hash("a\n"), start_line=1, end_line=5)


# --- block (tree-sitter) replacement -------------------------------------


class TestEditBlock:
    def test_replace_function(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        new = edit_file(
            str(f),
            _hash(SAMPLE),
            block="alpha",
            new_text='def alpha():\n    return "A"\n',
        )
        content = f.read_text()
        assert 'return "A"' in content
        assert "return 1" not in content
        assert new == _hash(content)

    def test_replace_nested_method(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        edit_file(
            str(f),
            _hash(SAMPLE),
            block="Canvas.draw",
            new_text='    def draw(self):\n        return "ink"\n',
        )
        assert 'return "ink"' in f.read_text()

    def test_other_blocks_survive(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        edit_file(
            str(f),
            _hash(SAMPLE),
            block="omega",
            new_text="def omega():\n    return 10\n",
        )
        content = f.read_text()
        assert "return 10" in content
        assert "return 1" in content  # alpha untouched

    def test_no_trailing_blank_line_inserted(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        # Replacing alpha with itself must reproduce the file byte-for-byte:
        # the block span excludes its trailing newline and the replacement
        # brings its own, so the original newline is consumed instead of
        # adding a blank line.
        edit_file(
            str(f),
            _hash(SAMPLE),
            block="alpha",
            new_text="def alpha():\n    return 1\n",
        )
        assert f.read_text() == SAMPLE

    def test_missing_block_raises(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        with pytest.raises(ValueError, match="no definition matching"):
            edit_file(str(f), _hash(SAMPLE), block="no_such_def")

    def test_stale_snapshot_rejected(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        with pytest.raises(RuntimeError, match="changed since it was read"):
            edit_file(str(f), _hash("stale"), block="alpha")

    def test_other_language(self, tmp_path):
        f = tmp_path / "mod.js"
        src = "function greet() {\n  return 1;\n}\n"
        f.write_text(src)
        edit_file(
            str(f),
            _hash(src),
            block="greet",
            new_text="function greet() {\n  return 2;\n}\n",
            language="javascript",
        )
        assert "return 2" in f.read_text()


class TestBlockEdgeCases:
    def test_last_block_without_trailing_newline(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def only():\n    return 1")  # no trailing newline
        edit_file(
            str(f),
            _hash(f.read_text()),
            block="only",
            new_text="def only():\n    return 2\n",
        )
        assert f.read_text() == "def only():\n    return 2\n"

    def test_new_text_without_trailing_newline_gets_one(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        edit_file(
            str(f), _hash(SAMPLE), block="alpha", new_text="def alpha():\n    return 3"
        )
        assert f.read_text() == SAMPLE.replace("return 1", "return 3")

    def test_block_direct_call(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(SAMPLE)
        snap = _edit_block(
            str(f), _hash(SAMPLE), "alpha", "def alpha():\n    return 7\n", "python"
        )
        assert "return 7" in f.read_text()
        assert snap == _hash(f.read_text())


# --- tree-sitter helpers -------------------------------------------------


class TestNodeHelpers:
    def test_node_name_none_without_name_field(self):
        tree = files_mod.get_parser("python").parse(b"pass")
        assert _node_name(tree.root_node) is None

    def test_node_name_of_function(self):
        src = b"def alpha():\n    pass\n"
        tree = files_mod.get_parser("python").parse(src)
        fn = _find_block_node(tree.root_node, ["alpha"])
        assert _node_name(fn) == "alpha"

    def test_node_body_prefers_body_field(self):
        src = b"def alpha():\n    pass\n"
        tree = files_mod.get_parser("python").parse(src)
        fn = _find_block_node(tree.root_node, ["alpha"])
        body = _node_body(fn)
        assert body is not None
        assert body.text.decode() == "pass"

    def test_node_body_falls_back_to_largest_named_child(self):
        # A decorated definition has no 'body' field; the fallback picks
        # its largest named child (the function definition).
        src = b"@decorator\ndef alpha():\n    pass\n"
        tree = files_mod.get_parser("python").parse(src)
        decorated = tree.root_node.named_children[0]
        body = _node_body(decorated)
        assert body.type == "function_definition"

    def test_node_body_none_without_children(self):
        tree = files_mod.get_parser("python").parse(b"x = 1\n")
        # The module node itself always has children; use an assignment's
        # identifier child which has no named children.
        assign = tree.root_node.named_children[0]
        ident = assign.named_children[0]
        assert _node_body(ident) is None

    def test_find_block_node_misses_unknown_name(self):
        tree = files_mod.get_parser("python").parse(b"def alpha():\n    pass\n")
        assert _find_block_node(tree.root_node, ["missing"]) is None

    def test_find_block_node_recurses_into_children(self):
        src = b"class Outer:\n    def method(self):\n        pass\n"
        tree = files_mod.get_parser("python").parse(src)
        node = _find_block_node(tree.root_node, ["Outer", "method"])
        assert node is not None
        assert _node_name(node) == "method"

    def test_find_block_node_descends_through_unnamed_wrappers(self):
        # A decorated function: the target lives under a node with no
        # matching name, exercising the else-branch recursion.
        src = b"@deco\ndef alpha():\n    pass\n"
        tree = files_mod.get_parser("python").parse(src)
        node = _find_block_node(tree.root_node, ["alpha"])
        assert node is not None
        assert _node_name(node) == "alpha"


# --- _edit_text / _edit_lines direct calls -------------------------------


class TestPrivateEditHelpers:
    def test_edit_lines_direct(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\n")
        snap = _edit_lines(str(f), _hash("a\nb\n"), 1, 1, "A")
        assert f.read_text() == "A\nb\n"
        assert snap == _hash("A\nb\n")

    def test_edit_text_direct(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("a\nb\n")
        snap = _edit_text(str(f), _hash("a\nb\n"), "a", "A", 0.8)
        assert f.read_text() == "A\nb\n"
        assert snap == _hash("A\nb\n")
