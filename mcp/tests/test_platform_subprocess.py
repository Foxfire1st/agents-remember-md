from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from agents_remember.kernel.platform_subprocess import (
    native_command,
    native_path_environment,
    native_subprocess_environment,
    windows_interop_reason,
)


def test_native_environment_uses_enclosure_reports_and_filters_windows_path(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "enclosure" / "reports"
    environment = native_subprocess_environment(
        {
            "PATH": os.pathsep.join(["/mnt/c/Program Files/nodejs", "/usr/local/bin", "/usr/bin"]),
            "TMP": "/mnt/c/Users/developer/AppData/Local/Temp",
        },
        temp_root=reports / "tmp",
        platform="posix",
    )

    assert environment["PATH"] == os.pathsep.join(["/usr/local/bin", "/usr/bin"])
    assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {
        (reports / "tmp").as_posix()
    }
    assert (reports / "tmp").is_dir()


def test_native_command_prefers_the_linux_tool_after_a_windows_path(tmp_path: Path) -> None:
    linux_bin = tmp_path / "linux-bin"
    linux_bin.mkdir()
    tool = linux_bin / "node"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    environment = {"PATH": os.pathsep.join(["/mnt/c/Program Files/nodejs", linux_bin.as_posix()])}

    with patch("agents_remember.kernel.platform_subprocess.os.name", "posix"):
        assert native_command(["node", "--version"], environment) == [
            tool.as_posix(),
            "--version",
        ]


def test_native_command_refuses_an_explicit_windows_shim() -> None:
    environment = {"PATH": "/usr/bin"}

    with pytest.raises(RuntimeError, match="Windows-mounted WSL path"):
        native_command(["/mnt/c/Program Files/nodejs/npm.cmd", "test"], environment)


def test_native_environment_refuses_windows_backed_temp_root() -> None:
    with pytest.raises(RuntimeError, match="Windows-mounted WSL path"):
        native_subprocess_environment(
            {"PATH": "/usr/bin"},
            temp_root=Path("/mnt/c/Users/developer/AppData/Local/Temp"),
            platform="posix",
        )


def test_windows_runner_keeps_its_environment_and_paths_unchanged(tmp_path: Path) -> None:
    source = {"PATH": r"C:\\Windows\\System32", "TEMP": r"C:\\Temp"}

    assert windows_interop_reason(r"C:\\tool.exe", platform="nt") is None
    assert native_path_environment(source, platform="nt") == source
    assert native_subprocess_environment(source, temp_root=tmp_path, platform="nt") == source
