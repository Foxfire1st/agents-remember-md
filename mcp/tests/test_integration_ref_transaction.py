"""Crash-cut forcing for the protected named-ref transaction owner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationPublicationIntent,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.worktrees.integration import integration_ref_transaction
from agents_remember.worktrees.integration.integration_ref_transaction import (
    CheckoutRefresh,
    IntegratedCommits,
    merge_integrated_commits,
    prepare_integration_ref_move,
    refresh_recovered_checkout,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.integrate import (
    IntegrationSources,
)
from agents_remember.worktrees.worktree_contract import write_contract
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_leaf_worktree,
)
from test_source_lineage import _git


class IntegrationRefTransactionTests(unittest.TestCase):
    @staticmethod
    def _land_external_recovery_pair(fixture, contract):
        lifecycle_operations.start_or_observe_operation(
            IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=contract.contract_path.as_posix(),
            ),
            contract,
            launcher=lambda *_: None,
        )
        store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
        runtime = OperationRuntime(store)
        running = runtime.start()
        authority = running.integrationAuthority
        assert authority is not None and contract.memory_repo_path is not None
        recovery = LifecycleOperationRecoveryCommits(
            codeCommit=contract.code_commit,
            memoryContentCommit=contract.memory_content_commit,
            ledgerCommit=contract.ledger_commit,
        )
        running = runtime.progress(
            "source-merge",
            {
                "current_command": "recover exact external integration pair",
                "irreversible_boundary": True,
                "recovery_commits": recovery.model_dump(mode="json"),
                "integration_publication": IntegrationPublicationIntent(
                    operationKey=running.operationKey,
                    generation=running.generation,
                    preparedAt="2026-08-22T00:00:00+00:00",
                    claimState="not-applicable",
                ).model_dump(mode="json"),
            },
        )
        _git(
            fixture.code_repo,
            "update-ref",
            f"refs/heads/{authority.codeSourceBranch}",
            recovery.codeCommit,
            authority.codeSourceCommit,
        )
        _git(
            contract.memory_repo_path,
            "update-ref",
            f"refs/heads/{authority.memorySourceBranch}",
            recovery.ledgerCommit,
            authority.memorySourceCommit,
        )
        durable = store.read()
        assert durable is not None
        return durable, recovery

    def test_post_cas_untracked_file_refuses_checkout_refresh_and_recovers_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(fixture, root, candidate_commit=True)
            write_contract(closed.contract_path, closed)
            lifecycle_operations.start_or_observe_operation(
                IntegrateOperationInput(
                    configPath=fixture.config_path.as_posix(),
                    contractPath=closed.contract_path.as_posix(),
                ),
                closed,
                launcher=lambda *_: None,
            )
            store = LifecycleOperationStore(
                operation_record_path(closed.worktree_group, "integrate")
            )
            running = OperationRuntime(store).start()
            authority = running.integrationAuthority
            assert authority is not None
            _git(fixture.code_repo, "switch", "main")
            owner = root / "super-owner"
            _git(fixture.code_repo, "worktree", "add", owner.as_posix(), "ar/master")
            commits = IntegratedCommits(code=closed.code_commit, memory_content="", ledger="")
            args = WorktreeArgs(operation_key=running.operationKey)
            snapshot = prepare_integration_ref_move(
                closed,
                commits,
                args,
                IntegrationSources(
                    current_code_source=authority.codeSourceCommit,
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
            )
            original = integration_ref_transaction._compare_and_swap_ref

            def create_untracked_after_cas(
                repo: Path,
                branch: str,
                expected: str,
                target: str,
                *,
                authority: object | None = None,
            ) -> bool:
                moved = original(
                    repo,
                    branch,
                    expected,
                    target,
                    authority=authority,
                )
                if moved:  # pragma: no cover
                    (owner / "concurrent-untracked.txt").write_text(
                        "keep\n", encoding="utf-8"
                    )  # pragma: no cover
                return moved

            with (
                mock.patch.object(
                    integration_ref_transaction,
                    "_compare_and_swap_ref",
                    side_effect=create_untracked_after_cas,
                ),
                self.assertRaisesRegex(RuntimeError, "untracked files"),
            ):
                merge_integrated_commits(closed, commits, snapshot)

            self.assertEqual(_git(fixture.code_repo, "rev-parse", "ar/master"), commits.code)
            self.assertTrue((owner / "concurrent-untracked.txt").exists())
            (owner / "concurrent-untracked.txt").unlink()
            refresh_recovered_checkout(
                closed,
                args,
                commits,
                CheckoutRefresh(
                    side="code",
                    old=snapshot.code_before,
                    new=commits.code,
                ),
            )
            self.assertEqual(_git(owner, "rev-parse", "HEAD"), commits.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
