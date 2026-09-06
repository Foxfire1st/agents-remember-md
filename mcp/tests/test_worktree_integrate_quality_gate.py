"""Altitude routing for the quality gate on integration (260731-EFA-L17-R2/R5)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.errors import CertificationProfileError
from agents_remember.models.lifecycles.operation import (
    IntegrationOperationAuthority,
    IntegrationPublicationIntent,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
    write_task_doc,
)
from agents_remember.worktrees.integration import integration_claim_transfer as claim_transfer_mod
from agents_remember.worktrees.integration import integration_quality as quality_mod
from agents_remember.worktrees.integration.integration_ref_transaction import IntegrationSources
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.modules import integrate as integrate_mod
from agents_remember.worktrees.modules import integration_recovery
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.gate import (
    GATE_FULL,
    GATE_TARGETED,
    QualityGatePlan,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    default_contract,
    default_series_contract,
    write_contract,
)
from integration_certification_test_support import integration_fixture
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    install_agents_remember_profile,
    install_fixture_profile,
)
from test_closeout_certification_entrypoint import _executor
from test_worktree_support import init_repo


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _not_applicable_integration_publication() -> IntegrationPublicationIntent:
    return IntegrationPublicationIntent(
        operationKey="a" * 64,
        generation=1,
        preparedAt="2026-08-15T00:00:00+00:00",
        claimState="not-applicable",
    )


def integration_contract(root: Path, *, kind: str = "leaf") -> WorktreeContract:
    coordination = root / "ar-coordination"
    repo = root / "repo"
    if not (repo / ".git").exists():
        init_repo(repo, "main")
        (repo / "ar-memory").mkdir()
        install_agents_remember_profile(repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "Add repository certification profile")
    base = git(repo, "rev-parse", "main")
    if not git(repo, "branch", "--list", "super"):
        git(repo, "branch", "super", "main")
    if kind == "series":
        task_name = "master"
        source_branch = "super"
        work_branch = "ar/master"
        if not git(repo, "branch", "--list", work_branch):
            git(repo, "branch", work_branch, source_branch)
        contract = default_series_contract(
            ContractTask(
                task_name,
                "agents-remember",
                coordination,
                "light-task",
                "internal",
                parent_task_name="sprint",
            ),
            code=RepoBranchPlan(repo, source_branch, work_branch, base),
        )
        contract = replace(
            contract,
            closeout_status="completed",
            approved_for_commit=True,
            human_review_status="approved",
            code_commit=git(repo, "rev-parse", work_branch),
        )
        master_nature = "atomic"
    else:
        task_name = "master-task"
        source_branch = "ar/master"
        work_branch = "ar/l1"
        if not git(repo, "branch", "--list", source_branch):  # pragma: no cover
            git(repo, "branch", source_branch, "main")
        contract = default_contract(
            ContractTask(
                task_name,
                "agents-remember",
                coordination,
                "light-task",
                "internal",
            ),
            leaf=LeafIdentity(worktree_name="l1", leaf_id="l1"),
            code=RepoBranchPlan(repo, source_branch, work_branch, base),
        )
        if not contract.code_worktree.exists():  # pragma: no cover
            contract.code_worktree.parent.mkdir(parents=True, exist_ok=True)
            git(
                repo,
                "worktree",
                "add",
                "-b",
                work_branch,
                str(contract.code_worktree),
                source_branch,
            )
        master_nature = "organizational"
    master_ref = TaskDocumentRef(
        repository="agents-remember",
        path=f"{task_name}/task.json",
    )
    write_task_doc(
        contract.task_root,
        TaskDocument.model_validate(
            {
                "id": task_name.upper(),
                "slug": task_name,
                "title": task_name,
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-08-15T00:00:00+00:00",
                "executionNature": master_nature,
                "subTasks": (
                    [
                        {
                            "number": "l1",
                            "name": "Leaf l1",
                            "file": "l1.md",
                            "status": "inProgress",
                        }
                    ]
                    if kind == "leaf"
                    else []
                ),
            }
        ),
    )
    write_task_doc(
        coordination / "tasks" / "agents-remember" / "sprint",
        TaskDocument.model_validate(
            {
                "id": "SPRINT",
                "slug": "sprint",
                "title": "Sprint",
                "kind": "master",
                "repo": "agents-remember",
                "createdAt": "2026-08-15T00:00:00+00:00",
                "orchestrates": [task_name],
                "integrationBranch": source_branch,
                "executionGraph": SprintExecutionGraph(nodes=[SprintExecutionNode(ref=master_ref)]),
            }
        ),
    )
    if kind == "leaf":
        write_task_doc(
            contract.task_root,
            TaskDocument.model_validate(
                {
                    "id": "l1",
                    "slug": "l1",
                    "title": "Leaf l1",
                    "kind": "subTask",
                    "repo": "agents-remember",
                    "createdAt": "2026-08-15T00:01:00+00:00",
                    "master": "task.md",
                }
            ),
        )
    write_contract(contract.contract_path, contract)
    publish_new_lifecycle_operation_location(
        contract,
        contract_text=contract.contract_path.read_text(encoding="utf-8"),
    )
    return contract


def external_recovery_contract(root: Path) -> WorktreeContract:
    return replace(
        integration_contract(root),
        memory_mode="external",
        memory_repo_path=root / "memory-repo",
        memory_source_branch="ar/master",
        memory_work_branch="ar/l1",
        memory_worktree=root / "worktrees/agents-remember/l1-ar/memory-l1",
        ledger_path=root / "worktrees/agents-remember/l1-ar/memory-l1/memory.md",
    )


def integration_authority() -> IntegrationOperationAuthority:
    return IntegrationOperationAuthority(
        targetKind="atomic-integration",
        codeRepository="/code.git",
        codeSourceBranch="ar/master",
        codeSourceRef="refs/heads/ar/master",
        codeSourceCommit="d" * 40,
        codeCandidateCommit="a" * 40,
    )


class IntegrationQualityGateAltitudeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_git_fixture_helper_surfaces_command_failures(self) -> None:
        with self.assertRaises(AssertionError):
            git(self.root, "definitely-not-a-git-command")

    def test_external_recovery_proves_the_exact_task_memory_head(self) -> None:
        contract = external_recovery_contract(self.root)
        commits = LifecycleOperationRecoveryCommits(
            codeCommit="a" * 40,
            memoryContentCommit="b" * 40,
            ledgerCommit="c" * 40,
        )
        with (
            mock.patch.object(integration_recovery, "require_clean"),
            mock.patch.object(integration_recovery, "head_commit", return_value="d" * 40),
            self.assertRaisesRegex(RuntimeError, "found task memory HEAD"),
        ):
            integration_recovery.prove_external_memory_recovery(contract, commits)
        with (
            mock.patch.object(integration_recovery, "require_clean"),
            mock.patch.object(
                integration_recovery,
                "head_commit",
                return_value=commits.ledgerCommit,
            ),
        ):
            integration_recovery.prove_external_memory_recovery(contract, commits)

    def test_completed_integration_recovery_must_match_exactly(self) -> None:
        current = integration_contract(self.root)
        landed = git(current.code_repo_path, "rev-parse", current.code_source_branch)
        contract = replace(
            current,
            integration_status="completed",
            integrated_code_commit=landed,
        )
        commits = LifecycleOperationRecoveryCommits(codeCommit=landed)
        args = WorktreeArgs(recovery_commits=commits)
        authority = integration_authority().model_copy(update={"codeCandidateCommit": landed})
        with mock.patch.object(integrate_mod, "status_payload", return_value={}):
            recovered = integrate_mod._recover_integration_finalization(
                contract,
                args,
                authority,
            )
        assert recovered is not None
        self.assertEqual(recovered.payload["state"], "already-integrated")
        with (
            mock.patch.object(
                integrate_mod,
                "_prove_integration_recovery_commits",
                return_value=integrate_mod.IntegratedCommits("a" * 40, "", ""),
            ),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            integrate_mod._recover_integration_finalization(
                replace(contract, integrated_code_commit="d" * 40),
                args,
                authority,
            )
        with mock.patch.object(
            integrate_mod, "_prove_integration_recovery_commits", return_value=None
        ):
            self.assertIsNone(
                integrate_mod._recover_integration_finalization(
                    replace(contract, integration_status="not-started"),
                    args,
                    authority,
                )
            )
        self.assertIsNone(
            integrate_mod._recover_integration_finalization(
                contract,
                WorktreeArgs(),
                authority,
            )
        )

    def test_integrate_result_refuses_completed_contract_without_durable_recovery(self) -> None:
        contract = replace(integration_contract(self.root), integration_status="completed")
        operation = SimpleNamespace(
            integrationAuthority=integration_authority(),
            recoveryCommits=None,
            integrationPublication=None,
        )
        with (
            mock.patch.object(integrate_mod, "load_contract", return_value=contract),
            mock.patch.object(
                integrate_mod,
                "require_plane_integration_operation",
                return_value=operation,
            ),
            mock.patch.object(integrate_mod, "status_payload", return_value={}),
            self.assertRaisesRegex(RuntimeError, "exact durable recovery evidence"),
        ):
            integrate_mod.integrate_result(
                WorktreeArgs(contract_path=contract.contract_path, approved=True),
                contract,
            )

    def test_integration_refusal_carries_one_explicit_recovery_command(self) -> None:
        contract = integration_contract(self.root, kind="leaf")
        recovery = {
            "nextOperation": "sync_source_lineage",
            "nextTool": "worktree_sync",
            "nextArgs": {"contract_path": contract.contract_path.as_posix()},
        }
        with (
            mock.patch.object(integrate_mod, "amend_contract", return_value=contract),
            mock.patch.object(integrate_mod, "write_contract"),
            mock.patch.object(integrate_mod, "status_payload", return_value={}),
        ):
            payload = integrate_mod.blocked_integration_payload(
                contract,
                "source-lineage-stale",
                "Sync before retrying integration.",
                **recovery,
            )

        self.assertEqual(
            payload["nextStep"],
            {"summary": "Sync before retrying integration.", **recovery},
        )

    def test_leaf_integration_reuses_closeout_acceptance_without_running_a_gate(self) -> None:
        contract = integration_contract(self.root, kind="leaf")

        with (
            mock.patch.object(
                quality_mod, "run_strict_code_quality_gate", return_value={"passed": True}
            ) as gate,
        ):
            result, blocked = integrate_mod._run_integration_quality_gate(
                contract,
                args=WorktreeArgs(certification_profile=AGENTS_REMEMBER_PROFILE_REFERENCE),
            )

        self.assertIsNone(blocked)
        self.assertFalse(result["required"])
        self.assertEqual(result["status"], "certified-at-leaf-closeout")
        self.assertEqual(result["mode"], GATE_TARGETED)
        gate.assert_not_called()

    def test_prepare_runs_the_altitude_gate_exactly_once_for_each_contract_kind(self) -> None:
        sources = IntegrationSources("c0", "", False, False)
        for kind in ("leaf", "series"):
            contract = integration_contract(self.root, kind=kind)
            with (
                self.subTest(kind=kind),
                mock.patch.object(
                    integrate_mod,
                    "_integrated_code_commit",
                    return_value=("c1", None),
                ),
                mock.patch.object(
                    integrate_mod,
                    "_run_integration_quality_gate",
                    return_value=({"passed": True}, None),
                ) as gate,
                mock.patch.object(
                    integrate_mod,
                    "_quality_gate_preview",
                    return_value={"status": "certified-at-leaf-closeout"},
                ) as preview,
                mock.patch.object(
                    integrate_mod,
                    "preview_integration_boundary",
                    return_value=integrate_mod.IntegrationBoundaryFacts(None, None, None),
                ),
                mock.patch.object(
                    integrate_mod,
                    "_integration_source_state_block",
                    return_value=None,
                ),
                mock.patch.object(
                    integrate_mod,
                    "_integrated_memory_commits",
                    return_value=("", "", None),
                ),
            ):
                prepared = integrate_mod._prepare_integration_commits(
                    contract,
                    WorktreeArgs(operation_key="a" * 64),
                    sources,
                )
            assert isinstance(prepared, tuple)
            self.assertEqual(prepared[0], integrate_mod.IntegratedCommits("c1", "", ""))
            if kind == "series":
                gate.assert_called_once_with(
                    contract,
                    args=WorktreeArgs(operation_key="a" * 64),
                )
                preview.assert_not_called()
            else:
                gate.assert_not_called()
                preview.assert_called_once_with(
                    contract,
                    profile_reference=None,
                )

    def test_master_integration_refuses_missing_profile_authority(
        self,
    ) -> None:
        contract = integration_contract(self.root, kind="series")

        with (
            mock.patch.object(
                integrate_mod,
                "_integrated_code_commit",
                return_value=("c1", None),
            ),
            mock.patch.object(integrate_mod, "write_contract"),
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract,
                WorktreeArgs(strategy="ff-only"),
                IntegrationSources(
                    current_code_source="c1",
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
                handover_warning=None,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.payload["state"], "blocked-quality-gate")
        self.assertEqual(
            result.payload["reason"],
            "integration refused by the required quality gate",
        )
        failure = cast(dict[str, object], result.payload["failureEvidence"])
        self.assertEqual(failure["stage"], "integration-quality-execution")
        self.assertEqual(failure["side"], "quality-gate")
        self.assertEqual(failure["name"], "integration-quality")
        self.assertEqual(
            result.payload["decisionSurface"],
            quality_mod.INTEGRATION_QUALITY_DECISION_SURFACE,
        )
        self.assertTrue(result.payload["developerDecisionRequired"])
        self.assertNotIn("wrapper", str(result.payload))
        merge.assert_not_called()

    def test_consumer_master_without_a_profile_is_blocked_before_execution(self) -> None:
        fixture = integration_fixture(self.root / "selected", contract_factory=integration_contract)
        with mock.patch.object(quality_mod, "run_strict_code_quality_gate") as gate:
            result, blocked = integrate_mod._run_integration_quality_gate(
                fixture.contract,
                args=WorktreeArgs(
                    certification_profile=None,
                    integration_certification_owner=fixture.owner,
                ),
            )
        self.assertEqual(result, {})
        self.assertIsNotNone(blocked)
        assert blocked is not None
        failure = cast(dict[str, object], blocked["failureEvidence"])
        self.assertEqual(failure["errorType"], "CertificationProfileError")
        gate.assert_not_called()

    def test_series_integration_runs_the_full_capped_gate(self) -> None:
        fixture = integration_fixture(self.root / "selected", contract_factory=integration_contract)
        contract = fixture.contract
        calls = []
        with (
            mock.patch.object(
                quality_mod,
                "quality_gate_settings",
                return_value=mock.Mock(memory_cap_bytes=2147483648),
            ) as settings,
            mock.patch.object(
                quality_mod,
                "run_strict_code_quality_gate",
                wraps=code_quality_gate.run_strict_code_quality_gate,
            ) as gate,
            mock.patch.object(
                code_quality_gate,
                "run_clean_quality",
                side_effect=_executor(NODE_FIXTURE, calls),
            ),
        ):
            result, blocked = integrate_mod._run_integration_quality_gate(
                contract,
                args=WorktreeArgs(
                    certification_profile=AGENTS_REMEMBER_PROFILE_REFERENCE,
                    integration_certification_owner=fixture.owner,
                ),
            )

        self.assertIsNone(blocked)
        self.assertTrue(result["passed"])
        (target,), kwargs = gate.call_args
        self.assertEqual(target.worktree_group, contract.worktree_group)
        self.assertEqual(target.repository_id, contract.repo_name)
        self.assertEqual(target.profile_reference, AGENTS_REMEMBER_PROFILE_REFERENCE)
        self.assertNotEqual(target.code_worktree, contract.code_repo_path)
        self.assertEqual(len(calls), 1)
        plan = kwargs["plan"]
        assert isinstance(plan, QualityGatePlan)
        self.assertEqual(plan.mode, GATE_FULL)
        self.assertEqual(plan.memory_cap_bytes, 2147483648)
        settings.assert_called_once_with(contract)
        self.assertEqual(kwargs["invocation"], "master-integration")

    def test_altitude_routing_is_kind_based(self) -> None:
        leaf = integration_contract(self.root, kind="leaf")
        series = integration_contract(self.root, kind="series")

        with self.assertRaisesRegex(ValueError, "reuses the exact leaf-closeout acceptance"):
            quality_mod.quality_gate_mode(leaf)
        self.assertEqual(quality_mod.quality_gate_mode(series), GATE_FULL)

    def test_series_preview_reads_the_exact_candidate_not_ambient_checkout(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "base.txt")
        git(repo, "commit", "-m", "base")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "switch", "-c", "atomic-candidate")
        install_fixture_profile(repo, "consumer-repo")
        git(repo, "add", AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix())
        git(repo, "commit", "-m", "candidate repository profile")
        candidate = git(repo, "rev-parse", "HEAD")
        git(repo, "switch", "main")
        contract = replace(
            integration_contract(self.root, kind="series"),
            repo_name="consumer-repo",
            code_repo_path=repo,
            code_base_commit=base,
            code_commit=candidate,
        )

        candidate_preview = integrate_mod._quality_gate_preview(
            contract,
            profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
        )

        self.assertTrue(candidate_preview["required"])
        install_fixture_profile(repo, "consumer-repo")
        with self.assertRaises(CertificationProfileError):
            integrate_mod._quality_gate_preview(
                replace(contract, code_commit=base),
                profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
            )

    def test_quality_gate_memory_cap_reads_the_settings_owned_value(self) -> None:
        contract = integration_contract(self.root, kind="series")
        settings = contract.coordination_root / "system" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {"orchestration": {"qualityGate": {"memoryCapBytes": 123456}}},
                indent=2,
            ),
            encoding="utf-8",
        )

        self.assertEqual(quality_mod.quality_gate_settings(contract).memory_cap_bytes, 123456)

    def test_quality_gate_memory_is_host_managed_when_the_cap_is_absent(self) -> None:
        contract = integration_contract(self.root, kind="series")

        self.assertIsNone(quality_mod.quality_gate_settings(contract).memory_cap_bytes)

    def test_a_refused_master_gate_blocks_integration_without_merging(self) -> None:
        fixture = integration_fixture(self.root / "selected", contract_factory=integration_contract)
        contract = fixture.contract
        private = "PRIVATE_QUALITY_BACKEND_STDERR /tmp/dagger stderr"

        def failing_gate(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError(private)

        with (
            mock.patch.object(quality_mod, "run_strict_code_quality_gate", failing_gate),
            mock.patch.object(integrate_mod, "write_contract"),
            mock.patch.object(
                integrate_mod,
                "_integrated_code_commit",
                return_value=("c1", None),
            ),
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract,
                WorktreeArgs(
                    strategy="ff-only",
                    certification_profile=AGENTS_REMEMBER_PROFILE_REFERENCE,
                    integration_certification_owner=fixture.owner,
                ),
                IntegrationSources(
                    current_code_source="c1",
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
                handover_warning=None,
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.payload["state"], "blocked-quality-gate")
        failure = cast(dict[str, object], result.payload["failureEvidence"])
        self.assertEqual(failure["errorType"], "RuntimeError")
        self.assertEqual(
            result.payload["decisionSurface"],
            quality_mod.INTEGRATION_QUALITY_DECISION_SURFACE,
        )
        self.assertTrue(result.payload["developerDecisionRequired"])
        self.assertNotIn(private, repr(result.payload))
        merge.assert_not_called()

    def test_premerge_blockers_return_without_crossing_the_source_boundary(self) -> None:
        contract = integration_contract(self.root, kind="leaf")
        sources = IntegrationSources("c0", "", False, False)
        code_block = {"state": "blocked-code-replay"}
        with (
            mock.patch.object(
                integrate_mod, "_integrated_code_commit", return_value=("", code_block)
            ),
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract, WorktreeArgs(strategy="ff-only"), sources, handover_warning=None
            )
        self.assertEqual(result.payload, code_block)
        merge.assert_not_called()

        memory_block = {"state": "blocked-memory-replay"}
        with (
            mock.patch.object(integrate_mod, "_integrated_code_commit", return_value=("c1", None)),
            mock.patch.object(
                integrate_mod,
                "_quality_gate_preview",
                return_value={"status": "certified-at-leaf-closeout"},
            ),
            mock.patch.object(integrate_mod, "_integration_source_state_block", return_value=None),
            mock.patch.object(
                integrate_mod,
                "_integrated_memory_commits",
                return_value=("", "", memory_block),
            ),
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract, WorktreeArgs(strategy="ff-only"), sources, handover_warning=None
            )
        self.assertEqual(result.payload, memory_block)
        merge.assert_not_called()

    def test_source_movement_after_quality_refuses_before_memory_or_merge(self) -> None:
        contract = integration_contract(self.root, kind="leaf")
        moved = integrate_mod.WorktreeCommandResult(2, {"state": "source-moved-during-quality"})

        with (
            mock.patch.object(integrate_mod, "_integrated_code_commit", return_value=("c1", None)),
            mock.patch.object(
                integrate_mod,
                "_quality_gate_preview",
                return_value={"status": "certified-at-leaf-closeout"},
            ),
            mock.patch.object(integrate_mod, "_integration_lineage_block", return_value=None),
            mock.patch.object(
                integrate_mod, "_integration_sources_moved_block", return_value=moved
            ) as source_check,
            mock.patch.object(integrate_mod, "_integrated_memory_commits") as memory,
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract,
                WorktreeArgs(strategy="ff-only"),
                IntegrationSources(
                    current_code_source="c0",
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
                handover_warning=None,
            )

        self.assertEqual(result.payload["state"], "source-moved-during-quality")
        source_check.assert_called_once()
        memory.assert_not_called()
        merge.assert_not_called()

    def test_source_movement_after_memory_resolution_refuses_before_merge(self) -> None:
        contract = integration_contract(self.root, kind="leaf")
        moved = integrate_mod.WorktreeCommandResult(2, {"state": "source-moved-during-quality"})

        with (
            mock.patch.object(integrate_mod, "_integrated_code_commit", return_value=("c1", None)),
            mock.patch.object(
                integrate_mod,
                "_quality_gate_preview",
                return_value={"status": "certified-at-leaf-closeout"},
            ),
            mock.patch.object(
                integrate_mod,
                "preview_integration_boundary",
                return_value=integrate_mod.IntegrationBoundaryFacts(None, None, None),
            ),
            mock.patch.object(
                integrate_mod, "_integration_source_state_block", side_effect=[None, moved]
            ) as source_check,
            mock.patch.object(
                integrate_mod,
                "_integrated_memory_commits",
                return_value=("", "", None),
            ),
            mock.patch.object(
                integrate_mod,
                "transfer_and_publish_integration_claim",
                return_value=_not_applicable_integration_publication(),
            ),
            mock.patch.object(
                integrate_mod,
                "prepare_integration_publication_intent",
                return_value=_not_applicable_integration_publication(),
            ),
            mock.patch.object(
                integrate_mod,
                "protected_integration_decision",
                return_value=None,
            ),
            mock.patch.object(integrate_mod, "merge_integrated_commits") as merge,
        ):
            result = integrate_mod._apply_integration(
                contract,
                WorktreeArgs(
                    strategy="ff-only",
                    operation_key="a" * 64,
                    operation_generation=1,
                ),
                IntegrationSources(
                    current_code_source="c0",
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
                handover_warning=None,
            )

        self.assertEqual(result.payload["state"], "source-moved-during-quality")
        self.assertEqual(source_check.call_count, 2)
        merge.assert_not_called()

    def test_source_tip_comparison_distinguishes_unchanged_and_moved(self) -> None:
        contract = integration_contract(self.root, kind="leaf")
        sources = IntegrationSources(
            current_code_source="c0",
            current_memory_source="",
            code_replay_required=False,
            memory_replay_required=False,
        )
        with mock.patch.object(integrate_mod, "branch_commit", return_value="c0"):
            self.assertIsNone(integrate_mod._integration_sources_moved_block(contract, sources))
        with (
            mock.patch.object(integrate_mod, "branch_commit", return_value="c1"),
            mock.patch.object(integrate_mod, "write_contract"),
        ):
            result = integrate_mod._integration_sources_moved_block(contract, sources)
        assert result is not None
        self.assertEqual(result.payload["state"], "source-moved-during-quality")


class IntegrationDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_reports_the_planned_gate_without_running_it(self) -> None:
        contract = integration_contract(self.root, kind="series")
        git(contract.code_repo_path, "checkout", contract.code_work_branch)
        candidate = git(contract.code_repo_path, "rev-parse", "HEAD")
        git(contract.code_repo_path, "checkout", "main")
        contract = replace(contract, code_commit=candidate)

        with (
            mock.patch.object(integrate_mod, "load_contract", return_value=contract),
            mock.patch.object(
                integrate_mod,
                "integration_targets",
                return_value=(SimpleNamespace(side="code", branch="ar/master"),),
            ),
            mock.patch.object(integrate_mod, "validate_integrate_contract"),
            mock.patch.object(integrate_mod, "_integration_door_block", return_value=None),
            mock.patch.object(integrate_mod, "_integration_lineage_block", return_value=None),
            mock.patch.object(
                integrate_mod,
                "_integration_replay_requirements",
                return_value=IntegrationSources(
                    current_code_source="c1",
                    current_memory_source="",
                    code_replay_required=False,
                    memory_replay_required=False,
                ),
            ),
            mock.patch.object(
                quality_mod,
                "quality_gate_settings",
                return_value=mock.Mock(memory_cap_bytes=999),
            ),
            mock.patch.object(quality_mod, "run_strict_code_quality_gate") as gate,
            mock.patch.object(integrate_mod, "write_contract"),
        ):
            result = integrate_mod.integrate_result(
                WorktreeArgs(
                    contract_path=contract.contract_path,
                    strategy="ff-only",
                    dry_run=True,
                    certification_profile=AGENTS_REMEMBER_PROFILE_REFERENCE,
                ),
                contract,
            )

        gate.assert_not_called()
        self.assertEqual(result.returncode, 0)
        quality_gate = result.payload["quality_gate"]
        assert isinstance(quality_gate, dict)
        self.assertEqual(quality_gate["mode"], GATE_FULL)
        memory_cap = quality_gate["memoryCap"]
        assert isinstance(memory_cap, dict)
        self.assertEqual(memory_cap["capBytes"], 999)

    def test_completed_dry_run_never_consumes_a_queue_candidate(self) -> None:
        contract = replace(
            integration_contract(self.root),
            integration_status="completed",
            integrated_code_commit="a" * 40,
        )
        with (
            mock.patch.object(integrate_mod, "load_contract", return_value=contract),
            mock.patch.object(integrate_mod, "require_ordinary_worktree"),
            mock.patch.object(integrate_mod, "integration_targets", return_value=()),
            mock.patch.object(integrate_mod, "status_payload", return_value={}),
            mock.patch.object(claim_transfer_mod, "transfer_integration_claim") as transfer,
        ):
            result = integrate_mod.integrate_result(
                WorktreeArgs(contract_path=contract.contract_path, dry_run=True),
                contract,
            )

        self.assertEqual(result.payload["state"], "already-integrated")
        transfer.assert_not_called()
