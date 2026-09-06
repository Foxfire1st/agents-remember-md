"""Candidate publication must recheck contract identity under closeout authority."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.worktrees.closeout_input import CloseoutCandidateSnapshot
from agents_remember.worktrees.modules import closeout
from closeout_input_test_support import MutationEvidenceRecorder, closeout_worktree_args
from integration_branch_authority_test_support import _authority_fixture


def test_closeout_candidate_publication_rechecks_the_contract(tmp_path: Path) -> None:
    fixture = _authority_fixture(tmp_path)
    contract = fixture.leaf_contract
    changed = replace(contract, cleanup="completed")
    with mock.patch(
        "agents_remember.worktrees.closeout_input.capture_closeout_candidate",
        return_value=CloseoutCandidateSnapshot("tree", "a" * 40, "tree"),
    ):
        args = closeout_worktree_args(
            contract,
            approved=True,
            approval_note="approved",
            candidate_tree="tree",
            operation_progress=MutationEvidenceRecorder(),
        )
    assert args.closeout_input is not None
    facts = closeout._CloseoutPublicationFacts(
        args=args,
        effective_input=args.closeout_input,
        worklist={"all": [], "working": [], "committed": []},
        quality=mock.Mock(spec=closeout._CloseoutQualityFacts),
        route_review={},
        approval_note="approved",
    )
    with (
        mock.patch.object(closeout, "load_contract", return_value=changed),
        mock.patch.object(
            closeout,
            "publish_closeout_under_authority",
            side_effect=lambda _contract, publication: publication(),
        ),
        mock.patch.object(closeout, "_closeout_commit_phase") as commit,
        mock.patch.object(closeout, "write_contract") as write,
        pytest.raises(RuntimeError, match="changed before candidate commit"),
    ):
        closeout._publish_closeout_candidate(contract, facts)
    commit.assert_not_called()
    write.assert_not_called()
