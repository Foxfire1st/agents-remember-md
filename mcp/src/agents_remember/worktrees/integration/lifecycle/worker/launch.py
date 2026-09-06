"""Stable failure publication for detached lifecycle-worker launch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_recovery_phase,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

OperationLauncher = Callable[[WorktreeContract, LifecycleOperationRecord], None]


def launch_or_fail(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    launcher: OperationLauncher,
    store: LifecycleOperationStore,
) -> None:
    """Launch once or persist one bounded task-addressed retry/recovery result."""

    try:
        launcher(contract, record)
    except Exception as error:
        stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
        failure = "detached lifecycle worker could not start"
        evidence = public_failure_evidence(
            stage="worker-launch",
            side="lifecycle-worker",
            name=record.operationKind,
            error_type=type(error).__name__,
            expected={"state": "running"},
            observed={"state": "launch-failed"},
        )
        recovery_phase = closeout_recovery_phase(record, waiting=True)
        retained = recovery_phase is not None
        next_action = "recover" if retained else "retry"
        result = {
            "state": "lifecycle-worker-launch-failed",
            "summary": failure,
            "failureEvidence": evidence,
            "nextAction": next_action,
        }

        def failed_launch(current: LifecycleOperationRecord) -> LifecycleOperationRecord:
            if current.status == "termination-required":
                return current
            return current.model_copy(
                update={
                    "status": "input-required" if retained else "failed",
                    "phase": recovery_phase or "failed",
                    "finishedAt": None if retained else stamp,
                    "failure": failure,
                    "result": result,
                    "guidance": (
                        "Fix the native runner environment, then recover the same exact "
                        "closeout generation."
                        if retained
                        else "Fix the native runner environment, then start the same task operation again."
                    ),
                }
            )

        store.update(failed_launch)
        raise LifecycleControlError(
            "lifecycle-worker-launch-failed",
            failure,
            expected={"state": "running", "operationKind": record.operationKind},
            observed={"failure": evidence},
            next_action=next_action,
        ) from error
