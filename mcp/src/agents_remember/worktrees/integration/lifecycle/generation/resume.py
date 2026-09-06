"""Pure same-generation lifecycle resume transition."""

from __future__ import annotations

from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)


def requeued_same_generation(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
    """Advance one retained generation without replacing immutable accepted intent."""
    history = list(record.workerTerminationHistory)
    termination = record.workerTermination
    if termination is not None:
        if termination.state != "exited":
            raise LifecycleControlError(
                "worker-termination-required",
                "worker termination must prove exit before retry or recover",
                next_action="cancel",
            )
        history.append(termination)
    mutations = dict(record.mutationEvidence)
    mutation_history = dict(record.mutationHistory)
    for leg, evidence in record.mutationEvidence.items():
        if evidence.state == "reconciled-unchanged":
            mutation_history[leg] = [*mutation_history.get(leg, []), evidence]
            mutations[leg] = evidence.model_copy(
                update={
                    "state": "pre-mutation",
                    "before": None,
                    "observed": None,
                    "expectedOutputTree": None,
                }
            )
    recovering_closeout = record.operationKind == "closeout" and closeout_generation_retained(
        record
    )
    return record.model_copy(
        update={
            "status": "running" if record.operationKind == "direct-landing" else "queued",
            "phase": (
                "direct-preflight"
                if record.operationKind == "direct-landing"
                else "recovering-after-claim"
                if recovering_closeout
                else "queued"
            ),
            "attempt": record.attempt + 1,
            "finishedAt": None,
            "failure": None,
            "guidance": None,
            "cancelRequested": False,
            "workerTermination": None,
            "workerTerminationHistory": history,
            "mutationEvidence": mutations,
            "mutationHistory": mutation_history,
            "currentCommand": "recover exact accepted generation",
        }
    )
