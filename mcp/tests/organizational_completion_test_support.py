"""Current door/journal fixture shared by organizational completion tests."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path

from _quality_evidence_fixture import publish_passing_quality_gate
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.tasks import read_task_doc, write_task_doc
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract
from closeout_input_test_support import (
    closeout_operation_input,
    publish_closeout_finalization,
    start_closeout_operation,
)
from test_closeout_queue import MASTER_A, QueueFixture
from test_worktree_support import git

FULL_GATE = {
    "required": True,
    "status": "enforced",
    "passed": True,
    "command": "./scripts/run-quality-gate --full",
    "diffBase": "base",
    "mode": "full",
    "executor": "dagger",
    "memoryPolicy": {
        "mode": "container-host-managed",
        "processPolicy": "profile-adapter-owned",
        "swap": "container-host-managed",
    },
    "reason": "exact full organizational acceptance passed",
}


def _full_gate(contract: WorktreeContract) -> Callable[..., dict[str, object]]:
    """Build a mock side effect that publishes exact passing organizational evidence."""

    def run(
        target: code_quality_gate.QualityGateTarget,
        *,
        diff_base: str = "",
        plan: code_quality_gate.QualityGatePlan | None = None,
        invocation: str = "master-integration",
        attestation: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        published = publish_passing_quality_gate(
            target,
            diff_base=diff_base,
            plan=plan,
            invocation=invocation,
            attestation=attestation,
        )
        return {
            **FULL_GATE,
            **published,
            "diffBase": diff_base or contract.code_base_commit,
        }

    return run


class OrganizationalCompletionFixture(unittest.TestCase):
    """Build one completed closeout through the current door and root journal."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = QueueFixture(self.path)

    @property
    def path(self):
        return Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _mark_master_work_complete(self) -> None:
        path = self.fixture.tasks / "master-a" / "task.json"
        master = read_task_doc(path)
        rows = [row.model_copy(update={"status": "Completed"}) for row in master.subTasks]
        write_task_doc(path.parent, master.model_copy(update={"subTasks": rows}))

    def _certified_contract(self, *, final: bool):
        if final:
            self._mark_master_work_complete()
        self.fixture.declare(MASTER_A)
        contract = self.fixture.contracts[MASTER_A]
        start_closeout_operation(
            closeout_operation_input(
                contract,
                config_path=self.fixture.config_path,
                code="close organizational leaf",
                memory="close organizational memory",
                ledger="map organizational pair",
                approval_note="approved",
            ),
            launcher=lambda *_: None,
        )
        store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
        runtime = lifecycle_operation_worker.OperationRuntime(store)
        runtime.start()
        closed = self.fixture.close_contract(MASTER_A)
        publish_closeout_finalization(runtime, closed)
        runtime.finish({"state": "closed"}, ok=True)
        git(closed.code_repo_path, "checkout", closed.code_source_branch)
        assert closed.memory_repo_path is not None
        git(closed.memory_repo_path, "checkout", closed.memory_source_branch)
        return load_contract(closed.contract_path)

    def _integration_runtime(self, contract):
        start_or_observe_operation(
            IntegrateOperationInput(
                configPath=self.fixture.config_path.as_posix(),
                contractPath=contract.contract_path.as_posix(),
                autoCompleteSeats=False,
            ),
            contract,
            launcher=lambda *_: None,
        )
        store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
        runtime = lifecycle_operation_worker.OperationRuntime(store)
        return store, runtime, runtime.start()

    @staticmethod
    def _args(contract, runtime, record) -> WorktreeArgs:
        return WorktreeArgs(
            contract_path=contract.contract_path,
            certification_profile=Path("mcp/certification-profile-v1.json"),
            approved=True,
            strategy="ff-only",
            operation_key=record.operationKey,
            operation_generation=record.generation,
            recovery_commits=record.recoveryCommits,
            quality_certification=record.qualityCertification,
            integration_publication=record.integrationPublication,
            operation_progress=runtime.progress,
        )
