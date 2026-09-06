"""Fail-closed normalized-input guards at closeout execution owners."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from agents_remember.worktrees.modules import cli as worktree_cli
from agents_remember.worktrees.modules import closeout
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.git import head_commit
from agents_remember.worktrees.queue import closeout_preview
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import MutationEvidenceRecorder, closeout_worktree_args
from test_closeout_queue import MASTER_A
from test_lifecycle_operations import _contract


def test_closeout_apply_refuses_missing_normalized_input_before_candidate_work(
    tmp_path: Path,
) -> None:
    contract = _contract(tmp_path)
    with pytest.raises(RuntimeError, match="normalized effective input"):
        closeout._effective_closeout_input(
            WorktreeArgs(
                contract_path=contract.contract_path,
                approved=True,
                approval_note="approved",
                operation_progress=MutationEvidenceRecorder(),
            ),
        )


def test_preview_refuses_missing_normalized_input(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    with pytest.raises(RuntimeError, match="preview requires normalized"):
        closeout_preview.proposed_closeout_commits(
            contract,
            WorktreeArgs(),
            False,
            False,
            {},
        )


def test_closeout_apply_requires_journaled_explicit_approval(tmp_path: Path) -> None:
    fixture = selected_fixture(tmp_path, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    before_head = head_commit(contract.code_worktree)
    recorder = MutationEvidenceRecorder()

    with pytest.raises(RuntimeError, match="journaled explicit commit approval"):
        closeout._closeout_approval_note(
            closeout_worktree_args(
                contract,
                operation_progress=recorder,
            ),
        )

    assert head_commit(contract.code_worktree) == before_head
    assert recorder.evidence == {}


def test_preview_only_cli_requires_task_addressing() -> None:
    with pytest.raises(RuntimeError, match="requires a contract path"):
        worktree_cli.command_closeout(Namespace(dry_run=True))
