"""Protected integration refs and their plane-owned mutation capability."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.worktrees.integration import (
    integration_ref_transaction,
)
from agents_remember.worktrees.integration.integration_branch_authority import (
    require_ordinary_worktree,
)
from agents_remember.worktrees.integration.integration_ref_transaction import (
    IntegratedCommits,
    IntegrationRefRace,
    merge_integrated_commits,
    prepare_integration_ref_move,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.integrate import (
    IntegrationSources,
    _recover_landed_refs,
)
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_external_leaf_worktrees,
)
from test_source_lineage import _commit_on, _git


class IntegrationBranchAuthorityTests(unittest.TestCase):
    def test_branch_alias_nested_checkout_and_memory_name_cannot_bypass_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            code = fixture.code_repo
            memory = fixture.leaf_contract.memory_repo_path
            assert memory is not None
            _git(code, "symbolic-ref", "refs/heads/alias-master", "refs/heads/ar/master")
            nested = code / "nested"
            nested.mkdir()
            cases = (
                replace(fixture.leaf_contract, code_work_branch="refs/heads/super"),
                replace(fixture.leaf_contract, code_work_branch="alias-master"),
                replace(
                    fixture.leaf_contract,
                    code_repo_path=nested,
                    code_work_branch="ar/atomic-two",
                ),
                replace(fixture.leaf_contract, memory_work_branch="main"),
            )
            for candidate in cases:
                with (
                    self.subTest(candidate=candidate),
                    self.assertRaisesRegex(RuntimeError, "integration-branch-is-not-a-workbench"),
                ):
                    require_ordinary_worktree(candidate, operation="test")

    def test_external_pair_cas_retains_torn_pair_without_clobbering_memory_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root, external_memory=True)
            closed = _closed_external_leaf_worktrees(fixture, root)
            memory_repo = closed.memory_repo_path
            assert memory_repo is not None
            operation_input = IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=closed.contract_path.as_posix(),
            )
            lifecycle_operations.start_or_observe_operation(
                operation_input,
                closed,
                launcher=lambda *_: None,
            )
            store = LifecycleOperationStore(
                operation_record_path(closed.worktree_group, "integrate")
            )
            running = OperationRuntime(store).start()
            authority = running.integrationAuthority
            assert authority is not None
            _git(memory_repo, "branch", "memory-race", "ar/master")
            _commit_on(memory_repo, "memory-race", "parallel-memory.txt")
            raced_memory = _git(memory_repo, "rev-parse", "memory-race")
            original_cas = integration_ref_transaction._compare_and_swap_ref

            def race_memory(
                repo: Path,
                branch: str,
                expected: str,
                target: str,
                *,
                authority: object | None = None,
            ) -> bool:
                if repo == memory_repo and branch == "ar/master":
                    _git(
                        memory_repo,
                        "update-ref",
                        "refs/heads/ar/master",
                        raced_memory,
                        expected,
                    )
                return original_cas(
                    repo,
                    branch,
                    expected,
                    target,
                    authority=authority,
                )

            with (
                mock.patch.object(
                    integration_ref_transaction,
                    "_compare_and_swap_ref",
                    side_effect=race_memory,
                ),
                self.assertRaises(IntegrationRefRace) as raised,
            ):
                commits = IntegratedCommits(
                    code=closed.code_commit,
                    memory_content=closed.memory_content_commit,
                    ledger=closed.ledger_commit,
                )
                snapshot = prepare_integration_ref_move(
                    closed,
                    commits,
                    WorktreeArgs(operation_key=running.operationKey),
                    IntegrationSources(
                        current_code_source=authority.codeSourceCommit,
                        current_memory_source=authority.memorySourceCommit,
                        code_replay_required=False,
                        memory_replay_required=False,
                    ),
                )
                merge_integrated_commits(closed, commits, snapshot)
            expected = raised.exception.expected
            self.assertEqual(
                expected["before"],
                {"codeRef": authority.codeSourceCommit, "memoryRef": authority.memorySourceCommit},
            )
            self.assertEqual(
                expected["intended"],
                {"codeRef": closed.code_commit, "memoryRef": closed.ledger_commit},
            )
            self.assertEqual(raised.exception.observed, {})
            self.assertEqual(
                _git(fixture.code_repo, "rev-parse", "ar/master"),
                closed.code_commit,
            )
            self.assertEqual(_git(memory_repo, "rev-parse", "ar/master"), raced_memory)

    def test_external_code_only_crash_completes_the_exact_memory_ref_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root, external_memory=True)
            closed = _closed_external_leaf_worktrees(fixture, root)
            memory_repo = closed.memory_repo_path
            assert memory_repo is not None
            operation_input = IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=closed.contract_path.as_posix(),
            )
            lifecycle_operations.start_or_observe_operation(
                operation_input,
                closed,
                launcher=lambda *_: None,
            )
            store = LifecycleOperationStore(
                operation_record_path(closed.worktree_group, "integrate")
            )
            running = OperationRuntime(store).start()
            authority = running.integrationAuthority
            assert authority is not None
            _git(
                fixture.code_repo,
                "update-ref",
                "refs/heads/ar/master",
                closed.code_commit,
                authority.codeSourceCommit,
            )

            recovered = _recover_landed_refs(
                closed,
                WorktreeArgs(operation_key=running.operationKey),
                LifecycleOperationRecoveryCommits(
                    codeCommit=closed.code_commit,
                    memoryContentCommit=closed.memory_content_commit,
                    ledgerCommit=closed.ledger_commit,
                ),
                authority,
            )

            self.assertTrue(recovered)
            self.assertEqual(
                _git(memory_repo, "rev-parse", "ar/master"),
                closed.ledger_commit,
            )
