"""Protected integration refs and their plane-owned mutation capability."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationPublicationIntent,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SprintExecutionNode,
    read_task_doc,
    write_task_doc,
)
from agents_remember.worktrees.integration import integration_branch_authority as branch_authority
from agents_remember.worktrees.integration import (
    integration_operation_authority,
    integration_ref_transaction,
    integration_topology_collisions,
)
from agents_remember.worktrees.integration.integration_branch_authority import (
    ProposedWorkBranches,
    canonical_local_branch,
    integration_surfaces,
    integration_targets,
    require_ordinary_worktree,
    require_proposed_work_branches,
    require_source_branch_write,
    require_sync_worktree,
)
from agents_remember.worktrees.integration.integration_operation_authority import (
    require_current_integration_sources,
    require_plane_integration_operation,
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
from agents_remember.worktrees.modules import start as start_module
from agents_remember.worktrees.modules.abandon import abandon_result
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.cleanup import cleanup_result
from agents_remember.worktrees.modules.closeout import closeout_result
from agents_remember.worktrees.modules.integrate import (
    IntegrationSources,
    _recover_landed_refs,
    integrate_result,
)
from agents_remember.worktrees.modules.start import attach_result
from agents_remember.worktrees.modules.startup import start_contract, start_memory
from agents_remember.worktrees.modules.sync import sync_result
from agents_remember.worktrees.worktree_contract import WorktreeContract, write_contract
from closeout_input_test_support import closeout_worktree_args
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_external_leaf_worktrees,
    _closed_leaf_worktree,
    _doc,
    _publish_completed_closeout_fixture,
)
from test_source_lineage import _commit_on, _git


class IntegrationBranchAuthorityTests(unittest.TestCase):
    def test_source_write_and_operation_authority_cover_exact_refusal_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            with self.assertRaisesRegex(RuntimeError, "protected code source"):
                require_source_branch_write(
                    fixture.leaf_contract,
                    side_name="code",
                    operation="test protected source",
                )
            require_source_branch_write(
                replace(
                    fixture.leaf_contract,
                    code_source_branch=fixture.leaf_contract.code_work_branch,
                ),
                side_name="code",
                operation="test ordinary source",
            )

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
            record = LifecycleOperationStore(
                operation_record_path(closed.worktree_group, "integrate")
            ).read()
            assert record is not None
            authority = record.integrationAuthority
            assert authority is not None
            cases = (
                (
                    closed,
                    authority.model_copy(update={"codeSourceRef": "refs/heads/wrong"}),
                    "code repository or target ref",
                ),
                (
                    replace(closed, code_worktree=root / "missing-code-worktree"),
                    authority,
                    "code candidate worktree",
                ),
                (
                    closed,
                    authority.model_copy(update={"memoryRepository": "/foreign-memory"}),
                    "internal-memory integration",
                ),
            )
            for changed, changed_authority, reason in cases:
                with self.subTest(reason=reason), self.assertRaisesRegex(RuntimeError, reason):
                    integration_operation_authority._require_contract_authority(
                        changed,
                        changed_authority,
                    )

    def test_live_leaf_collision_census_covers_terminal_and_removed_source_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            master_path = fixture.coordination / "tasks" / "repo" / "master" / "task.json"
            master = read_task_doc(master_path)
            write_task_doc(
                master_path.parent,
                master.model_copy(update={"executionNature": "organizational"}),
            )
            write_contract(
                fixture.master_contract.contract_path,
                replace(fixture.master_contract, cleanup="completed"),
            )
            cleaned = replace(fixture.leaf_contract, cleanup="completed")
            write_contract(cleaned.contract_path, cleaned)
            surfaces = integration_surfaces(cleaned)
            integration_topology_collisions.require_no_live_leaf_collisions(
                integration_topology_collisions.TopologyCollisionRequest(
                    scope=branch_authority._scope(cleaned),
                    current=surfaces,
                    candidate=surfaces,
                    overrides={},
                ),
                branch_authority._topology_collision_services(),
            )

            with mock.patch.object(
                integration_topology_collisions,
                "iter_leaf_enclosure_contracts",
                return_value=[fixture.master_contract.contract_path],
            ):
                integration_topology_collisions.require_no_live_leaf_collisions(
                    integration_topology_collisions.TopologyCollisionRequest(
                        scope=branch_authority._scope(cleaned),
                        current=surfaces,
                        candidate=surfaces,
                        overrides={},
                    ),
                    branch_authority._topology_collision_services(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            current = integration_surfaces(fixture.leaf_contract)
            source_key = (
                "code",
                branch_authority.repository_identity(fixture.code_repo),
                fixture.leaf_contract.code_source_branch,
            )
            candidate = tuple(
                surface
                for surface in current
                if (surface.side, surface.repository, surface.branch) != source_key
            )
            with (
                mock.patch.object(
                    integration_topology_collisions,
                    "_require_live_leaf_source_authority",
                ),
                self.assertRaisesRegex(RuntimeError, "source authority would be removed"),
            ):
                integration_topology_collisions.require_no_live_leaf_collisions(
                    integration_topology_collisions.TopologyCollisionRequest(
                        scope=branch_authority._scope(fixture.leaf_contract),
                        current=current,
                        candidate=candidate,
                        overrides={},
                    ),
                    branch_authority._topology_collision_services(),
                )

    def test_resolves_default_super_and_every_active_series_on_both_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)

            surfaces = integration_surfaces(fixture.leaf_contract)

            self.assertEqual(
                {(surface.side, surface.kind, surface.branch) for surface in surfaces},
                {
                    ("code", "repository-default", "main"),
                    ("code", "sprint-super", "super"),
                    ("code", "atomic-integration", "ar/master"),
                    ("code", "atomic-integration", "ar/atomic-two"),
                    ("memory", "repository-default", "main"),
                    ("memory", "sprint-super", "super"),
                    ("memory", "atomic-integration", "ar/master"),
                    ("memory", "atomic-integration", "ar/atomic-two"),
                },
            )

    def test_active_atomic_task_stays_protected_without_or_after_its_series_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            sibling_path = (
                fixture.coordination / "tasks" / "repo" / "atomic-two" / "series-contract.md"
            )
            sibling = start_contract.load_contract(sibling_path)
            sibling_path.unlink()
            missing = integration_surfaces(fixture.leaf_contract)
            self.assertIn("ar/atomic-two", {surface.branch for surface in missing})

            write_contract(sibling_path, replace(sibling, cleanup="completed"))
            completed = integration_surfaces(fixture.leaf_contract)
            self.assertIn("ar/atomic-two", {surface.branch for surface in completed})

    def test_organizational_master_refuses_a_live_legacy_series_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            master_path = fixture.coordination / "tasks" / "repo" / "master" / "task.json"
            master = read_task_doc(master_path)
            write_task_doc(
                master_path.parent,
                master.model_copy(update={"executionNature": "organizational"}),
            )

            with self.assertRaisesRegex(RuntimeError, "retains a live series contract"):
                integration_surfaces(fixture.leaf_contract)

    def test_standalone_atomic_parent_branch_is_protected_without_sprint_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            sprint_path = fixture.coordination / "tasks" / "repo" / "sprint" / "task.json"
            sprint = read_task_doc(sprint_path)
            sibling_ref = TaskDocumentRef(repository="repo", path="atomic-two/task.json")
            write_task_doc(
                sprint_path.parent,
                sprint.model_copy(
                    update={
                        "orchestrates": ["atomic-two"],
                        "executionGraph": SprintExecutionGraph(
                            nodes=[SprintExecutionNode(ref=sibling_ref)],
                            edges=[],
                        ),
                    }
                ),
            )
            memory_repo = fixture.master_contract.memory_repo_path
            assert memory_repo is not None
            standalone_series = replace(
                fixture.master_contract,
                code_source_branch="main",
                code_base_commit=_git(fixture.code_repo, "rev-parse", "main"),
                memory_source_branch="main",
                memory_base_commit=_git(memory_repo, "rev-parse", "main"),
            )
            write_contract(standalone_series.contract_path, standalone_series)
            fixture.master_contract = standalone_series

            surfaces = integration_surfaces(fixture.leaf_contract)

            self.assertEqual(
                {
                    (surface.side, surface.kind, surface.branch)
                    for surface in surfaces
                    if surface.kind == "atomic-integration"
                },
                {
                    ("code", "atomic-integration", "ar/master"),
                    ("memory", "atomic-integration", "ar/master"),
                    ("code", "atomic-integration", "ar/atomic-two"),
                    ("memory", "atomic-integration", "ar/atomic-two"),
                },
            )

    def test_corrupt_atomic_repository_or_memory_edge_fails_the_surface_census(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root, external_memory=True)
            sibling_path = (
                fixture.coordination / "tasks" / "repo" / "atomic-two" / "series-contract.md"
            )
            sibling = start_contract.load_contract(sibling_path)
            foreign = root / "foreign"
            foreign.mkdir()
            _git(foreign, "init", "-b", "main")
            _git(foreign, "config", "user.email", "test@example.invalid")
            _git(foreign, "config", "user.name", "Test")
            (foreign / "base.txt").write_text("base\n", encoding="utf-8")
            _git(foreign, "add", "base.txt")
            _git(foreign, "commit", "-m", "base")
            with self.subTest(edge="foreign-code"):
                write_contract(
                    sibling_path,
                    replace(
                        sibling,
                        code_repo_path=foreign,
                        code_worktree=foreign,
                    ),
                )
                with self.assertRaisesRegex(RuntimeError, "different repository"):
                    integration_surfaces(fixture.leaf_contract)
            with self.subTest(edge="missing-memory"):
                write_contract(
                    sibling_path,
                    replace(
                        sibling,
                        memory_mode="disabled",
                        memory_repo_path=None,
                        memory_worktree=None,
                        ledger_path=None,
                        memory_source_branch="",
                        memory_work_branch="",
                        memory_base_commit="",
                    ),
                )
                with self.assertRaisesRegex(RuntimeError, "repository memory edge"):
                    integration_surfaces(fixture.leaf_contract)

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

    def test_start_refuses_before_parent_series_bootstrap_or_worktree_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            context = SimpleNamespace(
                coordination_root=fixture.coordination,
                code_repository_name="repo",
                code_repository_root=fixture.code_repo,
                memory_mode="disabled",
            )
            args = WorktreeArgs(
                task_name="master",
                worktree_name="attempt",
                leaf_id="leaf-1",
                work_branch="refs/heads/super",
                memory_mode="disabled",
            )
            with (
                mock.patch.object(
                    start_contract,
                    "resolve_active_task_root",
                    return_value=fixture.leaf_contract.task_root,
                ),
                mock.patch.object(
                    start_contract, "resolve_start_leaf_doc_id", return_value="leaf-1"
                ),
                mock.patch.object(start_contract, "_parent_series_contract") as parent_series,
                self.assertRaisesRegex(RuntimeError, "integration-branch-is-not-a-workbench"),
            ):
                start_contract._build_start_contract(context, args)
            parent_series.assert_not_called()

    def test_persisted_malicious_contract_is_refused_by_attach_closeout_and_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            malicious = replace(fixture.leaf_contract, code_work_branch="super")
            write_contract(malicious.contract_path, malicious)
            for operation in (
                lambda: attach_result(WorktreeArgs(contract_path=malicious.contract_path)),
                lambda: closeout_result(
                    WorktreeArgs(contract_path=malicious.contract_path, dry_run=True),
                    malicious,
                ),
            ):
                with (
                    self.subTest(operation=operation),
                    self.assertRaisesRegex(RuntimeError, "integration-branch-is-not-a-workbench"),
                ):
                    operation()
            refused = sync_result(WorktreeArgs(contract_path=malicious.contract_path, dry_run=True))
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.payload["state"], "sync-operation-refused")
            detail = refused.payload["detail"]
            assert isinstance(detail, str)
            self.assertIn("integration-branch-is-not-a-workbench", detail)

    def test_series_sync_uses_canonical_authority_but_direct_integration_is_refused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            require_sync_worktree(fixture.master_contract)

            with self.assertRaisesRegex(RuntimeError, "plane-owned journaled"):
                integrate_result(
                    WorktreeArgs(
                        contract_path=fixture.leaf_contract.contract_path,
                        approved=True,
                    ),
                    fixture.leaf_contract,
                )

    def test_integration_record_pins_candidate_and_both_source_tips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root, external_memory=True)
            closed = _closed_external_leaf_worktrees(fixture, root)
            operation_input = IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=closed.contract_path.as_posix(),
            )
            lifecycle_operations.start_or_observe_operation(
                operation_input, closed, launcher=lambda *_: None
            )
            store = LifecycleOperationStore(
                operation_record_path(closed.worktree_group, "integrate")
            )
            runtime = OperationRuntime(store)
            running = runtime.start()
            authority = running.integrationAuthority
            assert authority is not None
            self.assertEqual(authority.codeSourceBranch, "ar/master")
            self.assertEqual(authority.codeCandidateCommit, closed.code_commit)
            self.assertEqual(authority.memorySourceBranch, "ar/master")
            args = WorktreeArgs(
                contract_path=closed.contract_path,
                approved=True,
                operation_key=running.operationKey,
            )
            self.assertEqual(require_plane_integration_operation(closed, args), running)
            self.assertEqual(
                require_current_integration_sources(
                    closed,
                    args,
                    code_source_commit=authority.codeSourceCommit,
                    memory_source_commit=authority.memorySourceCommit,
                ),
                running,
            )

            _commit_on(fixture.code_repo, "ar/master", "moved.txt")
            with self.assertRaisesRegex(RuntimeError, "code integration source moved"):
                require_current_integration_sources(
                    closed,
                    args,
                    code_source_commit=_git(fixture.code_repo, "rev-parse", "ar/master"),
                    memory_source_commit=authority.memorySourceCommit,
                )

    def test_proposed_branch_helper_refuses_code_and_memory_without_guessing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            memory_repo = fixture.leaf_contract.memory_repo_path
            assert memory_repo is not None
            for code_branch, memory_branch in (("main", "leaf-x"), ("leaf-x", "super")):
                with (
                    self.subTest(code=code_branch, memory=memory_branch),
                    self.assertRaisesRegex(RuntimeError, "integration-branch-is-not-a-workbench"),
                ):
                    require_proposed_work_branches(
                        ProposedWorkBranches(
                            coordination_root=fixture.coordination,
                            repo_name="repo",
                            task_root=fixture.leaf_contract.task_root,
                            code_repository=fixture.code_repo,
                            code_work_branch=code_branch,
                            memory_repository=memory_repo,
                            memory_work_branch=memory_branch,
                        )
                    )

    def test_missing_or_malformed_default_branch_authority_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            _git(
                fixture.code_repo,
                "symbolic-ref",
                "--delete",
                "refs/remotes/origin/HEAD",
            )
            _git(
                fixture.code_repo,
                "config",
                "agents-remember.defaultBranch",
                "main",
            )
            with self.assertRaisesRegex(RuntimeError, "default-branch authority is unavailable"):
                integration_surfaces(fixture.leaf_contract)

            _git(
                fixture.code_repo,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/heads/main",
            )
            with self.assertRaisesRegex(RuntimeError, "default-branch authority is malformed"):
                integration_surfaces(fixture.leaf_contract)

            _git(
                fixture.code_repo,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/missing",
            )
            with self.assertRaisesRegex(RuntimeError, "authority target does not exist"):
                integration_surfaces(fixture.leaf_contract)

    def test_symbolic_local_branch_git_error_fails_closed(self) -> None:
        failed = SimpleNamespace(returncode=128, stdout="", stderr="permission denied")
        with (
            mock.patch(
                "agents_remember.worktrees.integration.integration_branch_repository.run_git",
                return_value=failed,
            ),
            self.assertRaisesRegex(RuntimeError, "local branch authority is unreadable"),
        ):
            canonical_local_branch(Path("/repo"), "leaf")

    def test_cleanup_and_abandon_refuse_malicious_protected_work_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            contracts = (
                replace(
                    fixture.leaf_contract,
                    code_work_branch="super",
                    integration_status="completed",
                ),
                replace(
                    fixture.leaf_contract,
                    memory_work_branch="ar/atomic-two",
                    integration_status="completed",
                ),
            )
            for malicious in contracts:
                write_contract(malicious.contract_path, malicious)
                for operation in ("cleanup", "abandon"):
                    with (
                        self.subTest(contract=malicious, operation=operation),
                        self.assertRaisesRegex(
                            RuntimeError,
                            "integration-branch-is-not-a-workbench",
                        ),
                    ):
                        args = WorktreeArgs(
                            contract_path=malicious.contract_path,
                            approved=True,
                            force=operation == "abandon",
                        )
                        (cleanup_result if operation == "cleanup" else abandon_result)(args)

    def test_series_closeout_refuses_memory_only_workbench_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            memory_repo = fixture.master_contract.memory_repo_path
            assert memory_repo is not None
            _git(memory_repo, "switch", "ar/master")
            (memory_repo / "dirty.md").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "cannot create code, memory, or ledger"):
                closeout_result(
                    closeout_worktree_args(
                        fixture.master_contract,
                        dry_run=True,
                    ),
                    fixture.master_contract,
                )

    def test_ordinary_start_recovery_cannot_move_a_protected_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp), external_memory=True)
            with mock.patch.object(
                start_module,
                "_branch_freshness_findings",
                return_value=[{"side": "code", "state": "behind"}],
            ):
                block = start_module._stale_base_preflight(
                    SimpleNamespace(code_repository_name="repo-a"),
                    fixture.leaf_contract,
                    WorktreeArgs(stale_base_choice="fast-forward"),
                )
            assert block is not None
            self.assertEqual(
                block["retiredChoices"],
                ["fast-forward moves protected sources outside their landing plane"],
            )

            memory_repo = fixture.leaf_contract.memory_repo_path
            assert memory_repo is not None
            _git(memory_repo, "branch", "-D", "ar/master")
            with self.assertRaisesRegex(RuntimeError, "task-derived memory source branch"):
                start_memory._ensure_memory_source_branch(fixture.leaf_contract)

    def test_direct_main_target_is_rejected_before_operation_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            closed = replace(
                _closed_leaf_worktree(
                    fixture,
                    Path(tmp),
                    candidate_commit=False,
                    publish_closeout_evidence=False,
                ),
                closeout_status="completed",
                approved_for_commit=True,
                code_commit=_git(fixture.code_repo, "rev-parse", "leaf"),
            )
            write_contract(closed.contract_path, closed)
            closed = _publish_completed_closeout_fixture(
                fixture, closed, final_source_branch="main"
            )
            operation_input = IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=closed.contract_path.as_posix(),
            )
            with self.assertRaisesRegex(RuntimeError, "does not match task-derived target"):
                lifecycle_operations.start_or_observe_operation(
                    operation_input,
                    closed,
                    launcher=lambda *_: None,
                )

    def test_standalone_series_cannot_use_generic_integration_to_move_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = _authority_fixture(Path(tmp))
            sprint_path = fixture.coordination / "tasks" / "repo" / "sprint" / "task.json"
            sprint = read_task_doc(sprint_path)
            sibling_ref = TaskDocumentRef(repository="repo", path="atomic-two/task.json")
            write_task_doc(
                sprint_path.parent,
                sprint.model_copy(
                    update={
                        "orchestrates": ["atomic-two"],
                        "executionGraph": SprintExecutionGraph(
                            nodes=[SprintExecutionNode(ref=sibling_ref)],
                            edges=[],
                        ),
                    }
                ),
            )
            standalone = replace(
                fixture.master_contract,
                code_source_branch="main",
            )

            with self.assertRaisesRegex(RuntimeError, "repository-default landing"):
                integration_targets(standalone)

    def test_source_alias_retarget_invalidates_the_journaled_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(
                fixture,
                root,
                candidate_commit=True,
                publish_closeout_evidence=False,
            )
            _git(
                fixture.code_repo,
                "symbolic-ref",
                "refs/heads/integration-alias",
                "refs/heads/ar/master",
            )
            closed = _publish_completed_closeout_fixture(
                fixture, closed, final_source_branch="integration-alias"
            )
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
            _git(
                fixture.code_repo,
                "symbolic-ref",
                "refs/heads/integration-alias",
                "refs/heads/main",
            )

            with self.assertRaisesRegex(RuntimeError, "does not match task-derived target"):
                require_plane_integration_operation(
                    closed,
                    WorktreeArgs(
                        contract_path=closed.contract_path,
                        approved=True,
                        operation_key=running.operationKey,
                    ),
                )

    def test_replay_request_returns_exact_non_mutating_leaf_resolution_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(fixture, root, candidate_commit=False)
            _commit_on(fixture.code_repo, "ar/master", "parallel.txt")
            source_before = _git(fixture.code_repo, "rev-parse", "ar/master")
            candidate_before = _git(closed.code_worktree, "rev-parse", "HEAD")
            write_contract(closed.contract_path, closed)
            operation_input = IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=closed.contract_path.as_posix(),
                strategy="replay",
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

            result = integrate_result(
                WorktreeArgs(
                    contract_path=closed.contract_path,
                    approved=True,
                    strategy="replay",
                    operation_key=running.operationKey,
                ),
                closed,
            )

            self.assertEqual(result.payload["state"], "integration-resolution-required")
            conflict = result.payload["conflictTransaction"]
            assert isinstance(conflict, dict)
            self.assertEqual(conflict["codeWorktree"], closed.code_worktree.as_posix())
            self.assertTrue(conflict["codeReplayRequired"])
            self.assertEqual(_git(fixture.code_repo, "rev-parse", "ar/master"), source_before)
            self.assertEqual(_git(closed.code_worktree, "rev-parse", "HEAD"), candidate_before)

    def test_named_ref_cas_advances_target_even_if_ambient_checkout_switches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(fixture, root, candidate_commit=True)
            write_contract(closed.contract_path, closed)
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
            _git(fixture.code_repo, "switch", "main")
            main_before = _git(fixture.code_repo, "rev-parse", "main")

            commits = IntegratedCommits(
                code=closed.code_commit,
                memory_content="",
                ledger="",
            )
            snapshot = prepare_integration_ref_move(
                closed,
                commits,
                WorktreeArgs(operation_key=running.operationKey),
                IntegrationSources(
                    current_code_source=authority.codeSourceCommit,
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
            )
            merge_integrated_commits(closed, commits, snapshot)

            self.assertEqual(
                _git(fixture.code_repo, "rev-parse", "ar/master"),
                closed.code_commit,
            )
            self.assertEqual(_git(fixture.code_repo, "rev-parse", "main"), main_before)

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

    def test_hard_crash_reuses_authority_and_recovers_the_exact_landed_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(fixture, root, candidate_commit=True)
            write_contract(closed.contract_path, closed)
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
            runtime = OperationRuntime(store)
            running = runtime.start()
            authority = running.integrationAuthority
            assert authority is not None
            runtime.progress(
                "source-merge",
                {
                    "irreversible_boundary": True,
                    "recovery_commits": {
                        "codeCommit": closed.code_commit,
                        "memoryContentCommit": "",
                        "ledgerCommit": "",
                    },
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
                "refs/heads/ar/master",
                closed.code_commit,
                authority.codeSourceCommit,
            )
            future = datetime.now(UTC) + timedelta(seconds=60)

            with (
                mock.patch.object(lifecycle_operations.os, "killpg"),
                mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
            ):
                observed = lifecycle_operations.start_or_observe_operation(
                    operation_input,
                    closed,
                    now=future,
                )
            self.assertEqual(observed.status, "running")
            launch.assert_not_called()

            with mock.patch.object(
                lifecycle_operations.os,
                "killpg",
                side_effect=ProcessLookupError,
            ):
                lifecycle_operations.start_or_observe_operation(
                    operation_input,
                    closed,
                    launcher=lambda *_: None,
                    now=future,
                )
            retained = store.read()
            assert retained is not None
            self.assertEqual(retained.integrationAuthority, authority)
            self.assertEqual(retained.status, "running")
            self.assertEqual(retained.generation, running.generation)
            resumed = OperationRuntime(store).start()
            result = integrate_result(
                WorktreeArgs(
                    contract_path=closed.contract_path,
                    approved=True,
                    operation_key=resumed.operationKey,
                    operation_generation=resumed.generation,
                    recovery_commits=resumed.recoveryCommits,
                    integration_publication=resumed.integrationPublication,
                ),
                closed,
            )
            self.assertEqual(result.payload["state"], "integrated")
            self.assertTrue(result.payload["recovered"])

    def test_duplicate_worker_and_legacy_operation_record_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            closed = _closed_leaf_worktree(fixture, root, candidate_commit=True)
            write_contract(closed.contract_path, closed)
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
            OperationRuntime(store).start()
            with mock.patch.object(
                lifecycle_operation_worker.os,
                "getpid",
                return_value=999_999,
            ):
                duplicate = OperationRuntime(store).start()
            self.assertEqual(duplicate, store.read())

            payload = json.loads(store.path.read_text(encoding="utf-8"))
            payload["schemaVersion"] = "1.0"
            store.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "explicit worktree_legacy_operation bridge"):
                store.read()

    def test_series_bootstrap_restarts_from_fresh_source_after_partial_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = _authority_fixture(root)
            task_root = fixture.coordination / "tasks" / "repo" / "atomic-three"
            write_task_doc(
                task_root,
                _doc(
                    id="ATOMIC-THREE",
                    slug="atomic-three",
                    title="Atomic Three",
                    kind="master",
                    executionNature="atomic",
                ),
            )
            sprint_path = fixture.coordination / "tasks" / "repo" / "sprint" / "task.json"
            sprint = read_task_doc(sprint_path)
            atomic_three_ref = TaskDocumentRef(
                repository="repo",
                path="atomic-three/task.json",
            )
            assert sprint.executionGraph is not None
            write_task_doc(
                sprint_path.parent,
                sprint.model_copy(
                    update={
                        "orchestrates": [*sprint.orchestrates, "atomic-three"],
                        "executionGraph": sprint.executionGraph.model_copy(
                            update={
                                "nodes": [
                                    *sprint.executionGraph.nodes,
                                    SprintExecutionNode(ref=atomic_three_ref),
                                ]
                            }
                        ),
                    }
                ),
            )
            spec = start_contract.MasterSeriesContractSpec(
                coordination_root=fixture.coordination,
                repo_name="repo",
                code_repo=fixture.code_repo,
                memory_root=None,
                task_root=task_root,
                task_name="atomic-three",
                parent_task_name="sprint",
                protected_branch="super",
            )
            with (
                mock.patch.object(
                    start_contract,
                    "publish_new_lifecycle_operation_location",
                    side_effect=RuntimeError("crash after ref publication"),
                ),
                self.assertRaisesRegex(RuntimeError, "crash after ref publication"),
            ):
                start_contract.ensure_master_series_contract(spec)
            base = _git(fixture.code_repo, "rev-parse", "ar/atomic-three")
            _commit_on(fixture.code_repo, "super", "later-super.txt")
            advanced = _git(fixture.code_repo, "rev-parse", "super")

            recovered = start_contract.ensure_master_series_contract(spec)
            assert isinstance(recovered, WorktreeContract)

            self.assertNotEqual(advanced, base)
            self.assertEqual(recovered.code_base_commit, advanced)
            self.assertEqual(_git(fixture.code_repo, "rev-parse", "ar/atomic-three"), advanced)
            self.assertFalse(start_contract._master_series_bootstrap_record_path(spec).exists())
