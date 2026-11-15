"""Tests for dspy_agent_tools.files.io."""

import pytest

from dspy_agent_tools.files import delete_file, read_file, write_file
from dspy_agent_tools.files.io import _hash

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

    def test_snapshot_on_deleted_file_rejects_write(self, tmp_path):
        f = tmp_path / "f.txt"
        snapshot = write_file(str(f), "original\n")
        f.unlink()
        with pytest.raises(RuntimeError, match="changed since it was read"):
            write_file(str(f), "fresh\n", snapshot=snapshot)
        assert not f.exists()


class TestDeleteFile:
    def test_deletes_without_snapshot(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("bye\n")
        assert delete_file(str(f)) == f"Deleted {f}"
        assert not f.exists()

    def test_deletes_with_matching_snapshot(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("bye\n")
        delete_file(str(f), snapshot=_hash("bye\n"))
        assert not f.exists()

    def test_stale_snapshot_rejected(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("changed\n")
        with pytest.raises(RuntimeError, match="changed since it was read"):
            delete_file(str(f), snapshot=_hash("original\n"))
        assert f.exists()

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no such file to delete"):
            delete_file(str(tmp_path / "absent.txt"))

    def test_directory_rejected(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        with pytest.raises(ValueError, match="is a directory"):
            delete_file(str(d))
        assert d.exists()

    def test_symlink_to_directory_removes_link_only(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        link = tmp_path / "link"
        link.symlink_to(d, target_is_directory=True)
        delete_file(str(link))
        assert not link.is_symlink()
        assert d.exists()
