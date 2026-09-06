from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    contract_lifecycle_lease,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from closeout_input_test_support import (
    closeout_operation_input,
    start_closeout_operation,
    with_commit_proven,
    with_mutation_intent,
)
from lifecycle_control_test_support import (
    cancel_current_generation,
)
from selected_lifecycle_test_support import (
    selected_contract,
)


def _input(contract, *, message: str = "close L23") -> CloseoutOperationInput:
    return closeout_operation_input(contract, code=message)


def test_start_returns_immediately_and_duplicate_observes_one_launch(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path, candidate_file=("candidate.py", "VALUE = 1\n"))
    launches = []

    def launcher(loaded, record) -> None:
        launches.append((loaded.task_name, record.fingerprint))

    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    first = start_closeout_operation(
        _input(contract, message="  close L23  "), launcher=launcher, now=now
    )
    second = start_closeout_operation(_input(contract), launcher=launcher, now=now)

    assert first.status == second.status == "queued"
    assert len(launches) == 1
    assert "job" not in first.model_dump_json().lower()
    assert "pid" not in first.model_dump_json().lower()


def test_contract_lifecycle_lease_excludes_cross_kind_and_terminal_mutation(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)

    with contract_lifecycle_lease(contract):
        with pytest.raises(RuntimeError, match=r"integrate cannot proceed.*closeout"):
            require_lifecycle_operation_compatible(contract, operation_kind="integrate")
        with pytest.raises(RuntimeError, match=r"terminal mutation cannot proceed.*closeout"):
            require_lifecycle_operation_compatible(contract, operation_kind=None)
        require_lifecycle_operation_compatible(contract, operation_kind="closeout")


def test_cancel_before_boundary_proves_exit_before_releasing_worker_authority(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    worker_lease = "c" * 64
    worker_pid = 4242
    process_fingerprint = "d" * 64
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "startedAt": "2026-08-22T09:59:00+00:00",
                "heartbeatAt": "2026-08-22T10:00:00+00:00",
                "currentCommand": "validate lifecycle operation",
                "workerPid": worker_pid,
                "workerLease": worker_lease,
                "workerProcessFingerprint": process_fingerprint,
            }
        )
    )
    running = store.read()
    assert running is not None
    assert running.workerPid == worker_pid
    assert running.workerLease == worker_lease
    assert running.workerProcessFingerprint == process_fingerprint

    def prove_exit(request):
        persisted = store.read()
        assert persisted is not None
        assert persisted.workerPid == request.pid
        assert persisted.workerTermination is not None
        assert persisted.workerTermination.state == "requested"
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-22T10:00:00+00:00",
                "detail": "exact process exited",
            }
        )

    with (
        patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            return_value=None,
        ),
        patch(
            "agents_remember.worktrees.integration.lifecycle.control.cancellation."
            "signal_worker_and_prove_exit",
            side_effect=prove_exit,
        ),
    ):
        projection = cancel_current_generation(contract.contract_path, "closeout")

    current = store.read()
    assert projection.status == "cancelled"
    assert current is not None and current.workerPid is None
    assert current.workerTermination is not None
    assert current.workerTermination.state == "exited"


def test_cancel_after_boundary_refuses_without_making_approval_reusable(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path, candidate_file=("boundary.txt", "boundary\n"))
    operation_input = _input(contract)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(with_mutation_intent)
    store.update(
        lambda record: with_commit_proven(record).model_copy(
            update={"status": "running", "phase": "code-commit", "approvalClaimed": True}
        )
    )

    with pytest.raises(LifecycleControlError) as raised:
        cancel_current_generation(contract.contract_path, "closeout")
    assert raised.value.status == "lifecycle-immutable-output-recovery-required"
    assert raised.value.next_action == "recover"
    assert raised.value.observed["irreversibleBoundaryEntered"] is True
    mutation_evidence = raised.value.observed["mutationEvidence"]
    assert isinstance(mutation_evidence, dict)
    code_evidence = mutation_evidence["code"]
    assert isinstance(code_evidence, dict)
    assert code_evidence["state"] == "commit-proven"
    assert store.read().approvalClaimed is True  # type: ignore[union-attr]
