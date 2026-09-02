from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from agents_remember_test_support.code_quality import clean_room


def test_clean_room_cli_passes_explicit_candidate_and_memory_limit(tmp_path: Path) -> None:
    code = tmp_path / "code"
    group = tmp_path / "group"
    code.mkdir()
    completed = subprocess.CompletedProcess(["dagger"], 7, stdout="evidence\n", stderr="")

    with patch.object(clean_room, "run_clean_quality", return_value=completed) as run:
        result = clean_room.main(
            [
                "--code-worktree",
                str(code),
                "--worktree-group",
                str(group),
                "--repository-id",
                "fixture-repository",
                "--certification-profile",
                "mcp/certification-profile-v1.json",
                "--mode",
                "targeted",
                "--diff-base",
                "base-commit",
                "--memory-cap-bytes",
                "1024",
            ]
        )

    assert result == 7
    request = run.call_args.args[0]
    assert request.code_worktree == code.resolve()
    assert request.worktree_group == group.resolve()
    assert request.repository_id == "fixture-repository"
    assert request.profile_reference == Path("mcp/certification-profile-v1.json")
    assert request.mode == "targeted"
    assert request.diff_base == "base-commit"
    assert request.memory_cap_bytes == 1024


def test_clean_room_cli_reports_refusal_without_a_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(clean_room, "run_clean_quality", side_effect=RuntimeError("no engine")):
        result = clean_room.main(
            [
                "--worktree-group",
                str(tmp_path),
                "--repository-id",
                "fixture-repository",
                "--certification-profile",
                "mcp/certification-profile-v1.json",
            ]
        )

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err == "clean quality refused: no engine\n"


def test_clean_room_cli_allows_a_quiet_success(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(["dagger"], 0, stdout="", stderr="")
    with patch.object(clean_room, "run_clean_quality", return_value=completed):
        assert (
            clean_room.main(
                [
                    "--worktree-group",
                    str(tmp_path),
                    "--repository-id",
                    "fixture-repository",
                    "--certification-profile",
                    "mcp/certification-profile-v1.json",
                ]
            )
            == 0
        )


def test_clean_room_script_entry_exits_with_the_canonical_result(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(["dagger"], 0, stdout="", stderr="")
    argv = [
        "clean_room.py",
        "--worktree-group",
        str(tmp_path),
        "--repository-id",
        "fixture-repository",
        "--certification-profile",
        "mcp/certification-profile-v1.json",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch(
            "agents_remember.worktrees.modules.quality.clean_executor.run_clean_quality",
            return_value=completed,
        ),
        pytest.raises(SystemExit) as exited,
    ):
        runpy.run_path(Path(clean_room.__file__).as_posix(), run_name="__main__")
    assert exited.value.code == 0
