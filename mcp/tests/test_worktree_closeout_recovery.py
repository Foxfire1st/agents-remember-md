from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from agents_remember.models.closeout.input import (
    EffectiveCloseoutInput,
    EnabledCloseoutLeg,
    NotApplicableCloseoutLeg,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecoveryCommits
from agents_remember.worktrees.modules import closeout as closeout_module
from agents_remember.worktrees.modules import closeout_external
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.queue import closeout_recovery as closeout_recovery_journal
from agents_remember.worktrees.worktree_contract import WorktreeContract, write_contract
from closeout_input_test_support import MutationEvidenceRecorder, closeout_worktree_args
from test_worktree_support import git, open_external_contract_fixture


def _message_authority(
    *, contract_kind: str = "leaf", memory_mode: str = "internal"
) -> EffectiveCloseoutInput:
    disabled = NotApplicableCloseoutLeg(reason="verified-existing test output")
    external = memory_mode == "external"
    return EffectiveCloseoutInput.model_validate(
        {
            "route": "worktree",
            "contractKind": contract_kind,
            "memoryMode": memory_mode,
            "code": disabled,
            "memory": (
                EnabledCloseoutLeg(reason="external memory output", message="commit memory")
                if external
                else disabled
            ),
            "ledger": (
                EnabledCloseoutLeg(reason="external ledger output", message="commit ledger")
                if external
                else disabled
            ),
        }
    )


def _patch_external_refresh(stack: ExitStack, contract: WorktreeContract) -> None:
    services = SimpleNamespace(memory_quality=SimpleNamespace(check_groups=lambda: ([], [])))
    stack.enter_context(
        mock.patch.object(
            closeout_external,
            "contract_context",
            return_value=contract_context(contract),
        )
    )
    stack.enter_context(
        mock.patch.object(closeout_external, "refresh_onboarding_metadata", return_value=[])
    )
    stack.enter_context(
        mock.patch.object(
            closeout_external,
            "refresh_route_overview_metadata_for_context",
            return_value=[],
        )
    )
    stack.enter_context(
        mock.patch.object(
            closeout_external,
            "refresh_entity_fingerprints_for_context",
            return_value=[],
        )
    )
    stack.enter_context(
        mock.patch.object(closeout_external, "refresh_route_indexes_for_context", return_value={})
    )
    stack.enter_context(
        mock.patch.object(closeout_external, "worktree_services", return_value=services)
    )
    stack.enter_context(
        mock.patch.object(closeout_external, "run_memory_quality_phase", return_value={})
    )
    stack.enter_context(
        mock.patch.object(closeout_external, "combine_memory_quality", return_value={})
    )
    stack.enter_context(mock.patch.object(closeout_external, "worktree_dirty", return_value=False))


def _external_closeout_evidence() -> closeout_external.ExternalCloseoutEvidence:
    return closeout_external.ExternalCloseoutEvidence(
        memory_quality_before_refresh={},
        coherence_no_impact=closeout_external.CuratorCoherenceNoImpact(),
    )


class CloseoutRecoveryTests(unittest.TestCase):
    def test_recovery_refuses_a_copy_that_claims_another_contract_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = open_external_contract_fixture(Path(tmp))
            requested_path = Path(tmp) / "copied-contract.md"
            write_contract(requested_path, contract)

            with (
                mock.patch.object(closeout_module, "report_operation_progress"),
                mock.patch.object(
                    closeout_module,
                    "require_ordinary_worktree",
                ) as authority,
                mock.patch.object(
                    closeout_module,
                    "_recover_closeout_finalization",
                ) as recover,
                self.assertRaisesRegex(RuntimeError, "contract path does not match"),
            ):
                closeout_module._closeout_contract(
                    WorktreeArgs(
                        contract_path=requested_path,
                        operation_key="a" * 64,
                        operation_progress=MutationEvidenceRecorder(),
                    ),
                    contract,
                )

            authority.assert_not_called()
            recover.assert_not_called()

    def test_code_commit_recovery_proves_head_and_candidate_tree(self) -> None:
        contract = SimpleNamespace(kind="leaf", code_worktree=Path("/code"))
        args = WorktreeArgs(
            candidate_tree="b" * 40,
            recovery_commits=LifecycleOperationRecoveryCommits(codeCommit="a" * 40),
        )
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="c" * 40),
            self.assertRaisesRegex(RuntimeError, "does not match task HEAD"),
        ):
            closeout_recovery_journal.accepted_code_commit(
                contract,
                args,
                _message_authority(),
                strict_code_quality_required=True,
            )

        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="a" * 40),
            mock.patch.object(closeout_recovery_journal, "require_git", return_value="c" * 40),
            self.assertRaisesRegex(RuntimeError, "accepted candidate tree"),
        ):
            closeout_recovery_journal.accepted_code_commit(
                contract,
                args,
                _message_authority(),
                strict_code_quality_required=True,
            )

    def test_clean_claimed_code_commit_is_journaled_without_recommit(self) -> None:
        contract = SimpleNamespace(kind="leaf", code_worktree=Path("/code"))
        evidence: dict[str, object] = {}
        args = WorktreeArgs(
            candidate_tree="b" * 40,
            operation_progress=lambda _phase, found: evidence.update(found),
        )
        with (
            mock.patch.object(closeout_recovery_journal, "worktree_dirty", return_value=False),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="a" * 40),
            mock.patch.object(closeout_recovery_journal, "require_git", return_value="b" * 40),
        ):
            found = closeout_recovery_journal.accepted_code_commit(
                contract,
                args,
                _message_authority(),
                strict_code_quality_required=True,
            )
        self.assertEqual(found, "a" * 40)
        self.assertEqual(
            evidence["recovery_commits"],
            {
                "codeCommit": "a" * 40,
                "memoryContentCommit": "",
                "ledgerCommit": "",
            },
        )

    def test_series_closeout_reuses_the_clean_accepted_head_without_committing(self) -> None:
        contract = SimpleNamespace(
            kind="series",
            code_repo_path=Path("/repo"),
            code_worktree=Path("/ambient"),
            code_work_branch="ar/master",
        )
        args = WorktreeArgs(candidate_tree="b" * 40)
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean") as require_clean,
            mock.patch.object(
                closeout_recovery_journal, "branch_commit", return_value="a" * 40
            ) as branch_commit,
            mock.patch.object(closeout_recovery_journal, "require_git", return_value="b" * 40),
            mock.patch.object(
                closeout_recovery_journal, "commit_verified_staged"
            ) as commit_verified,
            mock.patch.object(closeout_recovery_journal, "commit_if_dirty") as commit_dirty,
        ):
            found = closeout_recovery_journal.accepted_code_commit(
                contract,
                args,
                _message_authority(contract_kind="series"),
                strict_code_quality_required=False,
            )

        self.assertEqual(found, "a" * 40)
        require_clean.assert_not_called()
        branch_commit.assert_called_once_with(contract.code_repo_path, contract.code_work_branch)
        commit_verified.assert_not_called()
        commit_dirty.assert_not_called()

        recovery_args = WorktreeArgs(
            recovery_commits=LifecycleOperationRecoveryCommits(codeCommit="c" * 40)
        )
        with (
            mock.patch.object(closeout_recovery_journal, "branch_commit", return_value="a" * 40),
            self.assertRaisesRegex(RuntimeError, "exact series ref"),
        ):
            closeout_recovery_journal.accepted_code_commit(
                contract,
                recovery_args,
                _message_authority(contract_kind="series"),
                strict_code_quality_required=False,
            )

    def test_external_resume_appends_history_and_rejects_invalid_head_or_ancestry(self) -> None:
        contract = SimpleNamespace(
            memory_worktree=Path("/memory"),
            ledger_path=Path("/memory/memory.md"),
            task_id="TASK",
        )
        args = WorktreeArgs()
        mapping = SimpleNamespace(memory_commit="b" * 40)
        intended = object()
        intent = object()
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "load_ledger", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="d" * 40),
            mock.patch.object(closeout_recovery_journal, "find_mapping", return_value=mapping),
            mock.patch.object(
                closeout_recovery_journal,
                "prepend_mapping",
                return_value=intended,
            ) as prepend,
            mock.patch.object(closeout_recovery_journal, "ledger_to_text", return_value="ledger"),
            mock.patch.object(
                closeout_recovery_journal,
                "begin_exact_file_git_mutation",
                return_value=intent,
            ),
            mock.patch.object(closeout_recovery_journal, "write_ledger"),
            mock.patch.object(closeout_recovery_journal, "require_git"),
            mock.patch.object(closeout_recovery_journal, "commit_if_dirty", return_value="e" * 40),
            mock.patch.object(closeout_recovery_journal, "prove_git_commit"),
        ):
            resumed_history = closeout_recovery_journal.resume_external_commits(
                contract,
                args,
                _message_authority(memory_mode="external"),
                code_commit="a" * 40,
                memory_commit="d" * 40,
            )
        self.assertEqual(resumed_history, ("d" * 40, "e" * 40))
        prepend.assert_called_once_with([], "a" * 40, "d" * 40)
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "load_ledger", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="c" * 40),
            mock.patch.object(closeout_recovery_journal, "find_mapping", return_value=None),
            self.assertRaisesRegex(RuntimeError, "memory HEAD"),
        ):
            closeout_recovery_journal.resume_external_commits(
                contract,
                args,
                _message_authority(memory_mode="external"),
                code_commit="a" * 40,
                memory_commit="b" * 40,
            )
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "load_ledger", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="c" * 40),
            mock.patch.object(closeout_recovery_journal, "find_mapping", return_value=mapping),
            mock.patch.object(closeout_recovery_journal, "is_ancestor", return_value=False),
            self.assertRaisesRegex(RuntimeError, "not reachable"),
        ):
            closeout_recovery_journal.resume_external_commits(
                contract,
                args,
                _message_authority(memory_mode="external"),
                code_commit="a" * 40,
                memory_commit="b" * 40,
            )
        evidence: dict[str, object] = {}
        with (
            mock.patch.object(closeout_recovery_journal, "require_clean"),
            mock.patch.object(closeout_recovery_journal, "load_ledger", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "head_commit", return_value="c" * 40),
            mock.patch.object(closeout_recovery_journal, "find_mapping", return_value=mapping),
            mock.patch.object(closeout_recovery_journal, "is_ancestor", return_value=True),
        ):
            resumed = closeout_recovery_journal.resume_external_commits(
                contract,
                replace(
                    args,
                    operation_progress=lambda _phase, found: evidence.update(found),
                ),
                _message_authority(memory_mode="external"),
                code_commit="a" * 40,
                memory_commit="b" * 40,
            )
        self.assertEqual(resumed, ("b" * 40, "c" * 40))
        self.assertEqual(
            evidence["recovery_commits"],
            {
                "codeCommit": "a" * 40,
                "memoryContentCommit": "b" * 40,
                "ledgerCommit": "c" * 40,
            },
        )

    def test_recovery_rejects_code_and_contract_memory_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = open_external_contract_fixture(Path(tmp))
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            no_memory = LifecycleOperationRecoveryCommits(codeCommit=code_head)
            with self.assertRaisesRegex(RuntimeError, "recorded code commit"):
                closeout_recovery_journal.prove_closeout_recovery_commits(
                    contract, LifecycleOperationRecoveryCommits(codeCommit="a" * 40)
                )

            internal = replace(
                contract,
                memory_mode="internal",
                memory_repo_path=None,
                memory_worktree=None,
                ledger_path=None,
            )
            self.assertEqual(
                closeout_recovery_journal.prove_closeout_recovery_commits(internal, no_memory),
                closeout_recovery_journal.MemoryCloseoutOutcome(),
            )
            with self.assertRaisesRegex(RuntimeError, "recorded external-memory commits"):
                closeout_recovery_journal.prove_closeout_recovery_commits(
                    internal,
                    LifecycleOperationRecoveryCommits(
                        codeCommit=code_head,
                        memoryContentCommit="b" * 40,
                        ledgerCommit="c" * 40,
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "requires memory worktree and ledger"):
                closeout_recovery_journal.prove_closeout_recovery_commits(
                    replace(contract, memory_worktree=None, ledger_path=None), no_memory
                )

    def test_recovery_rejects_unproven_memory_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = open_external_contract_fixture(Path(tmp))
            assert contract.memory_worktree is not None
            assert contract.ledger_path is not None
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            mapping = closeout_recovery_journal.find_mapping(
                closeout_recovery_journal.load_ledger(contract.ledger_path), code_head
            )
            assert mapping is not None
            proven = LifecycleOperationRecoveryCommits(
                codeCommit=code_head,
                memoryContentCommit=mapping.memory_commit,
                ledgerCommit=git(contract.memory_worktree, "rev-parse", "HEAD"),
            )
            self.assertEqual(
                closeout_recovery_journal.prove_closeout_recovery_commits(contract, proven),
                closeout_recovery_journal.MemoryCloseoutOutcome(
                    memory_commit=proven.memoryContentCommit,
                    ledger_commit=proven.ledgerCommit,
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "found memory HEAD"):
                closeout_recovery_journal.prove_closeout_recovery_commits(
                    contract, proven.model_copy(update={"ledgerCommit": "d" * 40})
                )
            for observed in (None, SimpleNamespace(memory_commit="e" * 40)):
                with (
                    self.subTest(observed=observed),
                    mock.patch.object(
                        closeout_recovery_journal, "find_mapping", return_value=observed
                    ),
                    self.assertRaisesRegex(RuntimeError, "ledger mapping"),
                ):
                    closeout_recovery_journal.prove_closeout_recovery_commits(contract, proven)
            with (
                mock.patch.object(closeout_recovery_journal, "is_ancestor", return_value=False),
                self.assertRaisesRegex(RuntimeError, "not reachable"),
            ):
                closeout_recovery_journal.prove_closeout_recovery_commits(contract, proven)

    def test_series_recovery_requires_memory_content_reachable_from_ledger(self) -> None:
        contract = cast(
            "WorktreeContract",
            SimpleNamespace(
                kind="series",
                memory_mode="external",
                code_repo_path=Path("/code"),
                code_work_branch="ar/master",
                memory_repo_path=Path("/memory"),
                memory_work_branch="ar/master",
            ),
        )
        commits = LifecycleOperationRecoveryCommits(
            codeCommit="a" * 40,
            memoryContentCommit="b" * 40,
            ledgerCommit="c" * 40,
        )
        with (
            mock.patch.object(
                closeout_recovery_journal,
                "branch_commit",
                side_effect=[commits.codeCommit, commits.ledgerCommit],
            ),
            mock.patch.object(closeout_recovery_journal, "require_git", return_value="ledger"),
            mock.patch.object(closeout_recovery_journal, "parse_ledger_text", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "_require_recovered_mapping"),
            mock.patch.object(closeout_recovery_journal, "is_ancestor", return_value=False),
            self.assertRaisesRegex(RuntimeError, "not reachable"),
        ):
            closeout_recovery_journal.prove_closeout_recovery_commits(contract, commits)

        with (
            mock.patch.object(
                closeout_recovery_journal,
                "branch_commit",
                side_effect=[commits.codeCommit, commits.ledgerCommit],
            ),
            mock.patch.object(closeout_recovery_journal, "require_git", return_value="ledger"),
            mock.patch.object(closeout_recovery_journal, "parse_ledger_text", return_value=[]),
            mock.patch.object(closeout_recovery_journal, "_require_recovered_mapping"),
            mock.patch.object(closeout_recovery_journal, "is_ancestor", return_value=True),
        ):
            outcome = closeout_recovery_journal.prove_closeout_recovery_commits(
                contract,
                commits,
            )
        self.assertEqual(
            outcome,
            closeout_recovery_journal.MemoryCloseoutOutcome(
                memory_commit=commits.memoryContentCommit,
                ledger_commit=commits.ledgerCommit,
            ),
        )

    def test_completed_recovery_must_match_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = open_external_contract_fixture(Path(tmp))
            code_head = git(contract.code_worktree, "rev-parse", "HEAD")
            internal = replace(
                contract,
                memory_mode="internal",
                memory_repo_path=None,
                memory_worktree=None,
                ledger_path=None,
                closeout_status="completed",
                code_commit=code_head,
            )
            args = WorktreeArgs(
                recovery_commits=LifecycleOperationRecoveryCommits(codeCommit=code_head)
            )
            with mock.patch.object(closeout_module, "status_payload", return_value={}):
                recovered = closeout_module._recover_closeout_finalization(internal, args)
            assert recovered is not None
            self.assertEqual(recovered.payload["state"], "already-closed")
            self.assertTrue(recovered.payload["recovered"])
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                closeout_module._recover_closeout_finalization(
                    replace(internal, code_commit="f" * 40), args
                )
            self.assertIsNone(
                closeout_module._recover_closeout_finalization(internal, WorktreeArgs())
            )

    def test_external_closeout_refuses_an_unreachable_existing_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            contract = open_external_contract_fixture(Path(tmp))
            _patch_external_refresh(stack, contract)
            stack.enter_context(
                mock.patch.object(
                    closeout_external,
                    "find_mapping",
                    return_value=SimpleNamespace(memory_commit="b" * 40),
                )
            )
            stack.enter_context(
                mock.patch.object(closeout_external, "head_commit", return_value="c" * 40)
            )
            stack.enter_context(
                mock.patch.object(closeout_external, "is_ancestor", return_value=False)
            )
            stack.enter_context(self.assertRaisesRegex(RuntimeError, "not reachable"))
            args = closeout_worktree_args(contract)
            assert args.closeout_input is not None
            closeout_external.external_closeout_commits(
                contract,
                args,
                args.closeout_input,
                closeout_module.VerifiedChange("a" * 40, "2026-08-14", ["feature.py"]),
                _external_closeout_evidence(),
            )

    def test_external_closeout_requires_ledger_and_memory_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = open_external_contract_fixture(Path(tmp))
            args = closeout_worktree_args(contract)
            assert args.closeout_input is not None
            change = closeout_module.VerifiedChange("a" * 40, "2026-08-22", [])
            with self.assertRaisesRegex(RuntimeError, "requires a ledger path"):
                closeout_external.external_closeout_commits(
                    replace(contract, ledger_path=None),
                    args,
                    args.closeout_input,
                    change,
                    _external_closeout_evidence(),
                )
            with self.assertRaisesRegex(RuntimeError, "requires a memory worktree"):
                closeout_external.external_closeout_commits(
                    replace(contract, memory_worktree=None),
                    args,
                    args.closeout_input,
                    change,
                    _external_closeout_evidence(),
                )

    def test_external_closeout_uses_clean_memory_head_when_no_mapping_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            contract = replace(
                open_external_contract_fixture(Path(tmp)),
                memory_content_commit="d" * 40,
            )
            _patch_external_refresh(stack, contract)
            stack.enter_context(
                mock.patch.object(closeout_external, "find_mapping", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(closeout_external, "head_commit", return_value="b" * 40)
            )
            stack.enter_context(mock.patch.object(closeout_external, "write_ledger"))
            stack.enter_context(mock.patch.object(closeout_external, "require_git"))
            ledger_intent = object()
            stack.enter_context(
                mock.patch.object(
                    closeout_external,
                    "begin_exact_file_git_mutation",
                    return_value=ledger_intent,
                )
            )
            stack.enter_context(mock.patch.object(closeout_external, "prove_git_commit"))
            stack.enter_context(
                mock.patch.object(closeout_external, "commit_if_dirty", return_value="c" * 40)
            )
            args = closeout_worktree_args(
                contract,
                approval_claimed=True,
                operation_progress=MutationEvidenceRecorder(),
            )
            assert args.closeout_input is not None
            result = closeout_external.external_closeout_commits(
                contract,
                args,
                args.closeout_input,
                closeout_module.VerifiedChange("a" * 40, "2026-08-14", ["feature.py"]),
                _external_closeout_evidence(),
            )

            self.assertEqual(result.memory_commit, "b" * 40)
            self.assertEqual(result.ledger_commit, "c" * 40)
