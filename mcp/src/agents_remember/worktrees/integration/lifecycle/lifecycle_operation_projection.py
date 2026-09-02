"""Pure public projection of one retained lifecycle operation generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationClassification,
    classify_door_publication,
)
from agents_remember.worktrees.integration.closeout.initial_door_recovery import (
    classify_initial_closeout_door_recovery,
)
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    classify_closeout_ledger_recovery,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_recovery_state import (
    classify_direct_landing_recovery,
)
from agents_remember.worktrees.integration.integration_operation_decision import (
    IntegrationOperationObservation,
    classify_integration_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    classify_migrated_lifecycle,
    public_lifecycle_evidence,
)
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    worker_exit_unproven,
    worker_termination_required_result,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class OperationProjectionContext:
    """Time, caller, and door evidence observed for one immutable projection."""

    now: datetime | None = None
    allow_completed_disposition: bool = False
    caller: DeclaredCaller | None = None
    door: DoorPublicationClassification | None = None


def operation_projection(
    record: LifecycleOperationRecord,
    *,
    contract: WorktreeContract | None = None,
    context: OperationProjectionContext | None = None,
) -> LifecycleOperationProjection:
    context = context or OperationProjectionContext()
    current = context.now or datetime.now(UTC)
    start = parse_operation_stamp(record.startedAt or record.queuedAt)
    finish = parse_operation_stamp(record.finishedAt) if record.finishedAt else current
    legal_controls: list[dict[str, object]] = []
    intent_unavailable = record.operationKind in {"closeout", "direct-landing"} and not isinstance(
        record.taskIntent, TaskIntentIdentity
    )
    legacy_intent_blocks_recovery = intent_unavailable and not (
        worker_exit_unproven(record) or _exit_proven_cancellation_pending(record)
    )
    integration_observation = (
        classify_integration_operation(contract, record)
        if contract is not None and record.operationKind == "integrate"
        else None
    )
    door_observation = _door_observation(record, contract, context)
    if contract is not None and not legacy_intent_blocks_recovery:
        from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (  # noqa: PLC0415
            LifecycleControlProjectionContext,
            legal_operation_controls,
        )

        legal_controls = legal_operation_controls(
            contract,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=context.allow_completed_disposition,
                caller=context.caller,
                integration=integration_observation,
                door=door_observation,
            ),
        )
    projected_result, projected_failure, projected_guidance = _projected_operation_result(
        record,
        contract,
        integration_observation,
        door_observation,
    )
    projected_result, projected_failure, projected_guidance = _legacy_intent_override(
        legacy_intent_blocks_recovery,
        projected_result,
        projected_failure,
        projected_guidance,
    )
    public_result = public_lifecycle_evidence(projected_result)
    if public_result is not None and not isinstance(public_result, dict):
        raise RuntimeError("lifecycle operation result must remain a public mapping")
    return LifecycleOperationProjection(
        kind=record.operationKind,
        status=record.status,
        phase=record.phase,
        startedAt=record.startedAt,
        heartbeatAt=record.heartbeatAt,
        finishedAt=record.finishedAt,
        elapsedSeconds=max(0.0, (finish - start).total_seconds()),
        currentCommand=f"lifecycle stage: {record.phase}",
        reportPath="" if record.legacyMigration is not None else record.reportPath,
        taskIntent=(
            record.taskIntent if isinstance(record.taskIntent, TaskIntentIdentity) else None
        ),
        result=public_result,
        failure=projected_failure,
        guidance=projected_guidance,
        cancellable=_operation_cancellable(
            record,
            contract=contract,
            intent_unavailable=legacy_intent_blocks_recovery,
            legal_controls=legal_controls,
        ),
        generation=record.generation,
        legalControls=legal_controls,
    )


def _door_observation(
    record: LifecycleOperationRecord,
    contract: WorktreeContract | None,
    context: OperationProjectionContext,
) -> DoorPublicationClassification | None:
    publication = record.doorPublication
    if (
        context.door is None
        and contract is not None
        and publication is not None
        and publication.state == "intent"
    ):
        return classify_door_publication(publication, contract)
    return context.door


def _exit_proven_cancellation_pending(record: LifecycleOperationRecord) -> bool:
    termination = record.workerTermination
    return bool(
        record.status == "termination-required"
        and record.cancelRequested
        and termination is not None
        and termination.state == "exited"
    )


def _legacy_intent_override(
    intent_unavailable: bool,
    projected_result: dict[str, Any] | None,
    projected_failure: str | None,
    projected_guidance: str | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not intent_unavailable:
        return projected_result, projected_failure, projected_guidance
    return (
        {
            "state": "lifecycle-operation-task-intent-unavailable",
            "summary": "The legacy operation predates canonical task intent.",
            "nextAction": "retire-and-republish",
        },
        "lifecycle-operation-task-intent-unavailable",
        (
            "A terminal generation may be retired and replaced by start/observe; "
            "an active generation requires a developer decision."
        ),
    )


def _operation_cancellable(
    record: LifecycleOperationRecord,
    *,
    contract: WorktreeContract | None,
    intent_unavailable: bool,
    legal_controls: list[dict[str, object]],
) -> bool:
    if intent_unavailable:
        return False
    if contract is not None:
        return any(item.get("action") == "cancel" for item in legal_controls)
    if record.status not in {"queued", "running", "input-required"}:
        return False
    if record.operationKind in {"closeout", "direct-landing"}:
        return not closeout_generation_retained(record)
    return not record.irreversibleBoundaryEntered


def _projected_operation_result(
    record: LifecycleOperationRecord,
    contract: WorktreeContract | None,
    integration_observation: IntegrationOperationObservation | None,
    door_observation: DoorPublicationClassification | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    general = _general_projected_result(record, integration_observation, door_observation)
    if general is not None:
        return general
    specific = _operation_specific_projected_result(record, contract)
    return specific or (record.result, record.failure, record.guidance)


def _general_projected_result(
    record: LifecycleOperationRecord,
    integration_observation: IntegrationOperationObservation | None,
    door_observation: DoorPublicationClassification | None,
) -> tuple[dict[str, Any] | None, str | None, str | None] | None:
    termination = worker_termination_required_result(record)
    if termination is not None:
        if record.cancelRequested and not worker_exit_unproven(record):
            surface = (
                "Exact worker exit is proven; complete the pending same-generation "
                "cancellation with the advertised task-addressed cancel action."
            )
            termination = {
                **termination,
                "reason": surface,
                "summary": surface,
                "expected": {
                    "state": "cancelled",
                    "workerAuthority": "released-after-exit-proof",
                },
            }
        else:
            surface = str(termination["summary"])
        return termination, surface, surface
    migrated = classify_migrated_lifecycle(record).recovery_result(record)
    if migrated is not None:
        return (
            migrated,
            "The migrated closeout generation requires exact same-generation recovery.",
            "Use the advertised task-addressed recover action.",
        )
    if door_observation is not None and door_observation.state == "developer-decision":
        result = door_observation.decision_payload()
        surface = str(result["decisionSurface"])
        return result, surface, surface
    return _integration_projected_result(integration_observation)


def _integration_projected_result(
    observation: IntegrationOperationObservation | None,
) -> tuple[dict[str, Any] | None, str | None, str | None] | None:
    if observation is None:
        return None
    if observation.projectedResult is not None:
        result = observation.projectedResult
        if observation.decision is not None:
            surface = str(result["decisionSurface"])
            return result, surface, surface
        return (
            result,
            str(result["reason"]),
            "Restart this exact task operation; live protected refs remain mechanically "
            "recoverable.",
        )
    if observation.repair.state == "reset":
        return (
            {
                "state": "organizational-completion-contract-reset",
                "summary": "the exact journal-owned repair reset is durably published",
            },
            None,
            "Observe the reopened task contract for its next lifecycle edge.",
        )
    return None


def _operation_specific_projected_result(
    record: LifecycleOperationRecord,
    contract: WorktreeContract | None,
) -> tuple[dict[str, Any] | None, str | None, str | None] | None:
    if contract is not None and record.operationKind == "closeout":
        initial_door = classify_initial_closeout_door_recovery(contract, record)
        ledger_recovery = classify_closeout_ledger_recovery(contract, record)
        if initial_door.state == "developer-decision":
            result = initial_door.decision_payload()
            return (
                result,
                str(result["decisionSurface"]),
                "Resolve the exact initial closeout-door contradiction.",
            )
        if ledger_recovery.state == "developer-decision":
            return (
                ledger_recovery.decision_payload(),
                ledger_recovery.detail,
                "Resolve the exact ledger byte/tree contradiction.",
            )
    if (
        contract is not None
        and record.operationKind == "direct-landing"
        and record.status not in {"completed", "cancelled"}
    ):
        direct_recovery = classify_direct_landing_recovery(contract, record)
        if direct_recovery.state == "developer-decision":
            return (
                direct_recovery.decision_payload(),
                direct_recovery.detail,
                "Resolve the exact direct-landing evidence contradiction.",
            )
    return None


def parse_operation_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
