"""Tests for dspy_agent_tools.wire.offload."""

from __future__ import annotations

from pathlib import Path

import pytest

from dspy_agent_tools import Offloader, Session


class TestOffloader:
    def test_small_value_unchanged(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=1000)
        assert offload.spill("x" * 1000) == "x" * 1000

    def test_spills_oversized_value(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=100, head=10, tail=5)
        text = "a" * 200

        handle = offload.spill(text, label="bash_exec")

        assert handle["offloaded"] is True
        assert handle["chars"] == 200
        assert handle["bytes"] == 200
        assert handle["head"] == "a" * 10
        assert handle["tail"] == "a" * 5
        assert Path(handle["path"]).read_text() == text
        assert "200 characters" in handle["note"]

    def test_content_addressed(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)

        first = offload.spill("y" * 50)
        second = offload.spill("y" * 50)

        assert first["path"] == second["path"]

    def test_label_is_sanitised(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)

        path = Path(offload.spill("z" * 50, label="../../weird name!")["path"])

        assert path.parent == tmp_path
        assert "/" not in path.name
        assert "!" not in path.name
        assert not path.name.startswith("-")

    def test_existing_handle_passes_through(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)
        handle = {"offloaded": True, "path": "kept"}

        assert offload.spill(handle) is handle

    def test_bytes_pass_through(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)
        data = b"x" * 100

        assert offload.spill(data) is data

    def test_non_string_value_is_json(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=10)

        handle = offload.spill({"a": ["b" * 100]})

        assert handle["offloaded"] is True
        assert Path(handle["path"]).read_text().startswith("{")

    def test_unserialisable_value_falls_back_to_repr(self, tmp_path: Path):
        offload = Offloader(tmp_path, limit=2)
        circular: list[object] = []
        circular.append(circular)

        handle = offload.spill(circular)

        assert handle["offloaded"] is True
        assert "..." in Path(handle["path"]).read_text()

    def test_session_records_artifact(self, tmp_path: Path):
        session = Session()
        offload = Offloader(tmp_path, limit=10, session=session)

        offload.spill("q" * 50, label="read_file")

        [artifact] = session.artifacts()
        assert artifact["bytes"] == 50
        assert artifact["note"] == "read_file"

    def test_invalid_configuration(self, tmp_path: Path):
        with pytest.raises(ValueError, match="limit must be positive"):
            Offloader(tmp_path, limit=0)
        with pytest.raises(ValueError, match="head must be positive"):
            Offloader(tmp_path, limit=10, head=0)
        with pytest.raises(ValueError, match="tail must be positive"):
            Offloader(tmp_path, limit=10, tail=0)
        with pytest.raises(ValueError, match="at least head"):
            Offloader(tmp_path, limit=10, head=6, tail=6)

    def test_samples_scale_down_with_limit(self):
        offload = Offloader(limit=100)

        assert offload.head == 50
        assert offload.tail == 50
