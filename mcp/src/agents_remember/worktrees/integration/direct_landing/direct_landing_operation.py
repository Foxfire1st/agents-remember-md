"""Durable synchronous coordinator for one direct-landing generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from agents_remember.models.lifecycles.direct_landing import (
    DirectLandingLedgerIntent,
    DirectLandingOperationInput,
)
from agents_remember.models.lifecycles.door import DoorPublicationEvidence
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationEvidence,
)
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_recovery_state import (
    classify_direct_landing_recovery,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    located_lifecycle_operation_report_path,
    located_lifecycle_operation_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import operation_key
from agents_remember.worktrees.integration.mutation_evidence import (
    initial_closeout_mutation_evidence,
    reconcile_closeout_mutations,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


class DirectLandingRuntime:
    """Advance one synchronous generation through the canonical operation store."""

    def __init__(self, contract: WorktreeContract, record: LifecycleOperationRecord) -> None:
        self.contract = contract
        self.store = direct_landing_store(contract)
        self.record = record

    def progress(self, phase: str, evidence: Mapping[str, object]) -> None:
        mutation_value = evidence.get("mutation_evidence")
        mutation = (
            GitMutationEvidence.model_validate(mutation_value)
            if mutation_value is not None
            else None
        )
        recovery_value = evidence.get("recovery_commits")
        reported = (
            LifecycleOperationRecoveryCommits.model_validate(recovery_value)
            if recovery_value is not None
            else None
        )
        stamp = _stamp()

        def advance(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
            mutations = dict(record.mutationEvidence)
            if mutation is not None:
                mutations[mutation.leg] = mutation
            recovery = derive_closeout_recovery_commits(
                record,
                mutations=mutations,
                reported=reported,
            )
            return record.model_copy(
                update={
                    "status": "running",
                    "phase": phase,
                    "heartbeatAt": stamp,
                    "currentCommand": str(evidence.get("current_command") or phase),
                    "mutationEvidence": mutations,
                    "recoveryCommits": recovery,
                    "irreversibleBoundaryEntered": any(
                        item.state == "commit-proven" for item in mutations.values()
                    ),
                }
            )

        self.record = self.store.update(advance)

    def publish_ledger_intent(self, intent: DirectLandingLedgerIntent) -> None:
        def publish(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
            current = record.directLandingLedgerIntent
            if current is not None and current != intent:
                raise RuntimeError("direct landing ledger intent is immutable once published")
            return record.model_copy(update={"directLandingLedgerIntent": intent})

        self.record = self.store.update(publish)

    def finish(self, result: dict[str, object]) -> LifecycleOperationRecord:
        stamp = _stamp()
        self.record = self.store.update(
            lambda record: record.model_copy(
                update={
                    "status": "completed",
                    "phase": "completed",
                    "heartbeatAt": stamp,
                    "finishedAt": stamp,
                    "currentCommand": "direct landing completed",
                    "result": result,
                    "failure": None,
                    "guidance": "The direct landing generation is complete.",
                }
            )
        )
        return self.record

    def require_input(
        self,
        *,
        status: str,
        detail: str,
        expected: Mapping[str, object] | None = None,
        observed: Mapping[str, object] | None = None,
        developer_decision: bool = False,
    ) -> LifecycleOperationRecord:
        stamp = _stamp()
        result: dict[str, object] = {
            "state": status,
            "nextAction": "developer-decision" if developer_decision else "recover",
            "expected": dict(expected or {}),
            "observed": dict(observed or {}),
        }
        if developer_decision:
            result.update(
                {
                    "developerDecisionRequired": True,
                    "decisionSurface": detail,
                }
            )
        self.record = self.store.update(
            lambda record: record.model_copy(
                update={
                    "status": "input-required",
                    "heartbeatAt": stamp,
                    "currentCommand": "direct landing recovery requires a decision",
                    "failure": f"{status}: {detail}",
                    "result": result,
                    "guidance": (
                        "Resolve the exact developer-decision evidence; status will advertise "
                        "recovery only after accepted or intended state is restored."
                        if developer_decision
                        else "Use the advertised task-addressed recover action after resolving "
                        "the exact observed evidence; do not repeat the landing from scratch."
                    ),
                }
            )
        )
        return self.record


def direct_landing_store(contract: WorktreeContract) -> LifecycleOperationStore:
    return located_lifecycle_operation_store(contract, "direct-landing")


def direct_landing_record(
    contract: WorktreeContract,
    operation_input: DirectLandingOperationInput,
    candidate: LifecycleOperationCandidate,
    door_publication: DoorPublicationEvidence | None,
) -> LifecycleOperationRecord:
    """Build a journal snapshot; callers attach the claim intent before persistence."""

    stamp = _stamp()
    return LifecycleOperationRecord(
        taskId=contract.task_id,
        taskName=contract.task_name,
        contractPath=contract.contract_path.as_posix(),
        operationKind="direct-landing",
        candidateState=candidate.state,
        candidateTree=candidate.tree,
        taskIntent=candidate.task_intent,
        fingerprint=candidate.fingerprint,
        operationKey=operation_key(
            contract.contract_path,
            "direct-landing",
            candidate.fingerprint,
        ),
        input=operation_input,
        status="running",
        phase="direct-preflight",
        queuedAt=stamp,
        startedAt=stamp,
        heartbeatAt=stamp,
        currentCommand="verify direct landing durable inputs",
        doorPublication=door_publication,
        reportPath=located_lifecycle_operation_report_path(
            contract,
            "direct-landing",
        ).as_posix(),
        mutationEvidence=initial_closeout_mutation_evidence(
            contract, operation_input.effectiveInput
        ),
        recoveryCommits=LifecycleOperationRecoveryCommits(codeCommit=operation_input.codeCommit),
    )


def reconcile_direct_landing(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
) -> LifecycleOperationRecord:
    """Reconcile launched Git commands and atomically project proven commits."""

    current = store.observe_current()
    for _attempt in range(3):
        if current is None or current.operationKind != "direct-landing":
            raise RuntimeError("direct landing operation record does not exist")
        reconciled = reconcile_closeout_mutations(current)
        recovery = derive_closeout_recovery_commits(current, mutations=reconciled)
        projected = current.model_copy(
            update={
                "mutationEvidence": reconciled,
                "recoveryCommits": recovery,
                "irreversibleBoundaryEntered": (
                    current.irreversibleBoundaryEntered
                    or any(item.state == "commit-proven" for item in reconciled.values())
                ),
            }
        )
        classification = classify_direct_landing_recovery(contract, projected)
        if classification.mechanically_convergent and classification.memory_commit:
            assert recovery is not None
            reported = LifecycleOperationRecoveryCommits(
                codeCommit=recovery.codeCommit,
                memoryContentCommit=classification.memory_commit,
                ledgerCommit=classification.ledger_commit,
            )
            recovery = derive_closeout_recovery_commits(
                projected,
                reported=reported,
            )
            projected = projected.model_copy(update={"recoveryCommits": recovery})
        if projected == current:
            return current
        updated, matched = store.update_if_current(
            current,
            lambda _record, projected=projected: projected,
        )
        if matched:
            return updated
        current = updated
    latest = store.observe_current() or current
    if latest is None:
        raise RuntimeError("direct landing operation disappeared during reconciliation")
    return latest


def reset_reconciled_attempt(
    store: LifecycleOperationStore,
    *,
    leg: CloseoutMutationLeg,
) -> LifecycleOperationRecord:
    """Archive an unchanged attempt before retrying that leg in the same generation."""

    def reset(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
        evidence = record.mutationEvidence.get(leg)
        if evidence is None or evidence.state != "reconciled-unchanged":
            return record
        history = dict(record.mutationHistory)
        history[leg] = [*history.get(leg, []), evidence]
        mutations = dict(record.mutationEvidence)
        mutations[leg] = evidence.model_copy(
            update={
                "state": "pre-mutation",
                "before": None,
                "observed": None,
                "expectedOutputTree": None,
            }
        )
        return record.model_copy(update={"mutationEvidence": mutations, "mutationHistory": history})

    return store.update(reset)


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
