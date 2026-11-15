"""Tests for dspy_agent_tools.skills.sandbox."""

from __future__ import annotations

import sys
from typing import Any, cast

import pytest

import dspy_agent_tools.skills.sandbox as sandbox_module
from dspy_agent_tools import Skills
from dspy_agent_tools.skills.sandbox import (
    ScriptSandbox,
    _family,
    bwrap_argv,
    firejail_argv,
    resolve_backend,
    sandbox_exec_profile,
)

from .helpers import SKILLS, by_name, copy_skill


class TestRunScript:
    def test_runs_a_script_with_arguments(self, monkeypatch):
        monkeypatch.setenv("SKILL_TEST_SECRET", "should-not-leak")
        tools = by_name(Skills(SKILLS))
        output = tools["run_skill_script"](
            skill="demo-skill", script="greet.py", args=["hello", "world"]
        )

        assert "greet: hello world" in output
        assert "secret: <unset>" in output
        assert f"cwd: {SKILLS / 'demo-skill'}" in output
        assert output.rstrip().endswith("s)")

    def test_code_mode_returns_structured_result(self):
        tools = by_name(Skills(SKILLS, mode="code"))

        record = tools["run_skill_script"](
            skill="demo-skill", script="greet.py", args=["hi"]
        )

        assert record["exit_code"] == 0
        assert record["timed_out"] is False
        assert record["sandbox"] in {"bwrap", "firejail", "sandbox-exec", "process"}
        assert "greet: hi" in record["output"]

    def test_non_zero_exit_is_reported(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="fail.py")

        assert "about to fail" in output
        assert "[exit code 3]" in output

    def test_timeout_kills_the_script(self):
        tools = by_name(Skills(SKILLS, timeout=0.5))

        output = tools["run_skill_script"](skill="demo-skill", script="slow.py")

        assert "timed out after 0.5s" in output

    def test_skill_without_scripts_dir(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="second-skill", script="anything.py")

        assert "has no scripts/ directory" in output

    def test_empty_script_name(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="")

        assert "script name is required" in output

    def test_missing_script_lists_alternatives(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="nope.py")

        assert "Script 'nope.py' not found" in output
        assert "greet.py" in output

    def test_rejects_scripts_outside_the_scripts_folder(self):
        tools = by_name(Skills(SKILLS))

        output = tools["run_skill_script"](skill="demo-skill", script="../SKILL.md")
        absolute = tools["run_skill_script"](skill="demo-skill", script="/etc/passwd")

        assert "escapes demo-skill/scripts" in output
        assert "must be relative" in absolute

    def test_interpreter_allowlist(self):
        tools = by_name(Skills(SKILLS, interpreters=("bash",)))

        output = tools["run_skill_script"](skill="demo-skill", script="greet.py")

        assert "Interpreter 'python' is not allowed" in output

    def test_unknown_interpreter_extension(self, tmp_path):
        skill = copy_skill(tmp_path)
        (skill / "scripts" / "data.bin").write_bytes(b"\x00\x01")
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](skill="demo-skill", script="data.bin")

        assert "Cannot determine an interpreter" in output


@pytest.mark.skipif(resolve_backend("auto") is None, reason="no OS sandbox installed")
class TestSandboxEnforcement:
    def test_skill_folder_is_read_only_by_default(self, tmp_path):
        skill = copy_skill(tmp_path)
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](skill="demo-skill", script="write_here.py")

        assert "PermissionError" in output or "Operation not permitted" in output
        assert not (skill / "written-by-skill.txt").exists()

    def test_allow_write_permits_the_skill_folder(self, tmp_path):
        skill = copy_skill(tmp_path)
        tools = by_name(Skills(tmp_path, allow_write=True))

        output = tools["run_skill_script"](skill="demo-skill", script="write_here.py")

        assert "wrote written-by-skill.txt" in output
        assert (skill / "written-by-skill.txt").read_text() == "ok"

    def test_scripts_cannot_write_outside_the_skill(self, tmp_path):
        copy_skill(tmp_path)
        outside = tmp_path / "escaped.txt"
        tools = by_name(Skills(tmp_path))

        output = tools["run_skill_script"](
            skill="demo-skill", script="escape.py", args=[str(outside)]
        )

        assert not outside.exists()
        assert "PermissionError" in output or "Operation not permitted" in output


class TestSandboxConstruction:
    def test_off_uses_process_confinement(self):
        assert resolve_backend("off") is None

    def test_unknown_sandbox_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown sandbox"):
            resolve_backend(cast(Any, "docker"))

    def test_unavailable_backend_raises(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        with pytest.raises(ValueError, match="not available"):
            resolve_backend("bwrap")

    def test_required_without_a_backend_raises(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        with pytest.raises(ValueError, match="no OS sandbox is installed"):
            resolve_backend("required")

    def test_bwrap_argv(self, tmp_path):
        argv = bwrap_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=False,
            allow_write=True,
        )

        assert argv[:2] == ["bwrap", "--unshare-all"]
        assert "--share-net" not in argv
        assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind") :][:3]
        assert ["--bind", str(tmp_path), str(tmp_path)] == argv[argv.index("--bind") :][
            :3
        ]
        assert argv[-3:] == ["--", "python", "s.py"]

    def test_firejail_argv(self, tmp_path):
        argv = firejail_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=False,
            allow_write=False,
        )

        assert argv[0] == "firejail"
        assert "--net=none" in argv
        assert "--read-only=/" in argv
        assert f"--working-directory={tmp_path}" in argv

    def test_sandbox_exec_profile(self, tmp_path):
        profile = sandbox_exec_profile(
            tmp_path,
            tmp_path / "tmp",
            allow_network=False,
            allow_write=False,
        )

        assert "(deny file-write*)" in profile
        assert "(deny network*)" in profile
        assert str((tmp_path / "tmp").resolve()) in profile

    def test_sandbox_requires_a_positive_timeout(self):
        with pytest.raises(ValueError, match="timeout"):
            ScriptSandbox(timeout=0)

    def test_script_outside_skill_is_rejected(self, tmp_path):
        sandbox = ScriptSandbox(sandbox="off")
        skill = copy_skill(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_text("print('x')")

        with pytest.raises(ValueError, match="outside the skill directory"):
            sandbox.run(outside, [], skill_dir=skill)

    def test_shebang_scripts_are_supported(self, tmp_path):
        skill = copy_skill(tmp_path)
        script = skill / "scripts" / "hello"
        script.write_text(f"#!{sys.executable}\nprint('shebang ok')\n")
        sandbox = ScriptSandbox(sandbox="off", interpreters=("python",))

        result = sandbox.run(script, [], skill_dir=skill)

        assert result.exit_code == 0
        assert "shebang ok" in result.output


class TestSandboxInternals:
    def test_family_normalization(self):
        assert _family("python3.13") == "python"
        assert _family("python.exe") == "python"
        assert _family("bash") == "bash"

    def test_sandbox_exec_is_unavailable_off_darwin(self, monkeypatch):
        monkeypatch.setattr(sandbox_module.sys, "platform", "linux")

        assert sandbox_module._available("sandbox-exec") is False

    def test_explicit_backend_that_is_installed(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: True)

        assert resolve_backend("bwrap") == "bwrap"

    def test_required_picks_the_first_installed_backend(self, monkeypatch):
        monkeypatch.setattr(
            sandbox_module, "_available", lambda backend: backend == "firejail"
        )

        assert resolve_backend("required") == "firejail"

    def test_auto_falls_back_to_process_confinement(self, monkeypatch):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: False)

        assert resolve_backend("auto") is None

    def test_bwrap_network_flag(self, tmp_path):
        argv = bwrap_argv(
            ["python", "s.py"],
            tmp_path,
            allow_network=True,
            allow_write=False,
        )

        assert "--share-net" in argv

    def test_configured_output_truncation(self):
        tools = by_name(Skills(SKILLS, max_output_chars=20))

        output = tools["run_skill_script"](skill="demo-skill", script="loud.py")

        assert "chars omitted" in output

    def test_constructor_rejects_bad_limits(self):
        with pytest.raises(ValueError, match="max_output_chars"):
            ScriptSandbox(max_output_chars=-1)
        with pytest.raises(ValueError, match="interpreters"):
            ScriptSandbox(interpreters=())

    def test_missing_interpreter_on_path(self, tmp_path):
        skill = tmp_path / "s"
        (skill / "scripts").mkdir(parents=True)
        script = skill / "scripts" / "run"
        script.write_text("#!/usr/bin/env golang\nprint(1)\n")
        sandbox = ScriptSandbox(sandbox="off", interpreters=("golang",))

        with pytest.raises(FileNotFoundError, match="Interpreter not found"):
            sandbox.run(script, [], skill_dir=skill)

    def test_shebang_edge_cases(self, tmp_path):
        assert ScriptSandbox._shebang(tmp_path / "missing") == (None, None)

        empty = tmp_path / "empty"
        empty.write_text("")
        assert ScriptSandbox._shebang(empty) == (None, None)

        bare = tmp_path / "bare"
        bare.write_text("#!\n")
        assert ScriptSandbox._shebang(bare) == (None, None)

        flags = tmp_path / "flags"
        flags.write_text("#!/usr/bin/env -i\n")
        assert ScriptSandbox._shebang(flags) == (None, None)

    def test_wrap_uses_the_selected_backend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sandbox_module, "_available", lambda backend: True)

        assert (
            ScriptSandbox(sandbox="bwrap")._wrap(["python", "x"], tmp_path, None)[0]
            == "bwrap"
        )
        assert (
            ScriptSandbox(sandbox="firejail")._wrap(["python", "x"], tmp_path, None)[0]
            == "firejail"
        )

    def test_missing_script_file_raises(self, tmp_path):
        skill = copy_skill(tmp_path)

        with pytest.raises(FileNotFoundError, match="Script not found"):
            ScriptSandbox(sandbox="off").run(
                skill / "scripts" / "missing.py", [], skill_dir=skill
            )

    def test_kill_race_is_tolerated(self, monkeypatch, tmp_path):
        skill = copy_skill(tmp_path)
        (skill / "scripts" / "nap.py").write_text("import time; time.sleep(0.3)\n")
        sandbox = ScriptSandbox(sandbox="off", timeout=0.05)

        def gone(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(sandbox_module.os, "killpg", gone)

        result = sandbox.run(skill / "scripts" / "nap.py", [], skill_dir=skill)

        assert result.timed_out is True
