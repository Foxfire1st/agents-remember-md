"""Failed workers preserve queue ownership after truthful irreversible evidence."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
    with_commit_proven,
    with_mutation_intent,
)
from selected_lifecycle_test_support import (
    completed_selected_closeout_for_integration,
    selected_contract,
)


def test_failed_closeout_with_commit_proof_keeps_queue_ownership(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path, candidate_file=("candidate.txt", "candidate\n"))
    start_closeout_operation(closeout_operation_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(with_mutation_intent)
    record = store.update(with_commit_proven)
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    config = load_config(Path(record.input.configPath))
    with (
        mock.patch.object(lifecycle_operation_worker, "load_config", return_value=config),
        mock.patch.object(
            lifecycle_operation_worker,
            "execute_selected_closeout",
            return_value=WorktreeCommandResult(1, {"state": "blocked"}),
        ),
    ):
        lifecycle_operation_worker.execute_operation(record, runtime)

    current = store.read()
    assert current is not None and current.status == "input-required"
    assert "queueReleaseFailure" not in (current.result or {})


def test_failed_irreversible_integration_keeps_queue_ownership(tmp_path: Path) -> None:
    contract = completed_selected_closeout_for_integration(selected_contract(tmp_path))
    operation_input = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(operation_input, contract, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "integrate"))
    record = store.update(
        lambda current: current.model_copy(update={"irreversibleBoundaryEntered": True})
    )
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    config = load_config(Path(record.input.configPath))
    with (
        mock.patch.object(lifecycle_operation_worker, "load_config", return_value=config),
        mock.patch.object(
            lifecycle_operation_worker,
            "integrate_result",
            return_value=WorktreeCommandResult(1, {"state": "blocked"}),
        ),
    ):
        lifecycle_operation_worker.execute_operation(record, runtime)

    current = store.read()
    assert current is not None and current.status == "input-required"
    assert "queueReleaseFailure" not in (current.result or {})
