"""Pure public projection of one retained lifecycle operation generation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.operation_projection import (
    LifecycleApprovalObservation,
    LifecycleProjectionComponentBindings,
    LifecycleProjectionIdentity,
    LifecycleProjectionIncoherence,
    LifecycleProjectionWorkerState,
    LifecycleRecommendedAction,
    LifecycleWorkerObservation,
    validate_projection_state,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
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
    """Time, caller, door, and revision-coupled evidence for one projection."""

    now: datetime | None = None
    allow_completed_disposition: bool = False
    caller: DeclaredCaller | None = None
    door: DoorPublicationClassification | None = None
    doorIdentity: LifecycleProjectionIdentity | None = None
    resultIdentity: LifecycleProjectionIdentity | None = None
    approvalIdentity: LifecycleProjectionIdentity | None = None
    workerIdentity: LifecycleProjectionIdentity | None = None


@dataclass(frozen=True)
class _ProjectionComponents:
    legal_controls: list[dict[str, Any]]
    result: dict[str, Any] | None
    failure: str | None
    guidance: str | None
    worker: LifecycleWorkerObservation
    recommendation: LifecycleRecommendedAction | None
    intent_unavailable: bool


def bind_projection_result(
    projection: LifecycleOperationProjection,
    result: dict[str, Any],
    *,
    guidance: str | None = None,
    recommendation: LifecycleRecommendedAction | None = None,
) -> LifecycleOperationProjection:
    """Compose a current-revision result through the projection's sole validator."""

    updates: dict[str, Any] = {
        "result": result,
        "recommendedAction": recommendation,
    }
    if guidance is not None:
        updates["guidance"] = guidance
    return _rebind_projection(projection, updates)


def bind_projection_decision(
    projection: LifecycleOperationProjection,
    result: dict[str, Any],
    detail: str,
) -> LifecycleOperationProjection:
    """Compose one exact read-only developer decision without residual controls."""

    return _rebind_projection(
        projection,
        {
            "result": result,
            "failure": detail,
            "guidance": detail,
            "recommendedAction": LifecycleRecommendedAction(
                action="developer-decision",
                summary=detail,
            ),
            "cancellable": False,
            "legalControls": [],
        },
    )


def _rebind_projection(
    projection: LifecycleOperationProjection,
    updates: dict[str, Any],
) -> LifecycleOperationProjection:
    identity = projection.identity
    if identity is None:
        raise LifecycleProjectionIncoherence(
            {"identity": "exact readable journal revision"},
            {"identity": "absent"},
        )
    payload = projection.model_dump(mode="python")
    payload.update(updates)
    digest = identity.identityDigest
    payload["componentBindings"] = LifecycleProjectionComponentBindings(
        result=digest if payload.get("result") is not None else None,
        approval=digest if payload.get("approval") is not None else None,
        worker=digest if payload.get("worker") is not None else None,
        recommendedAction=(digest if payload.get("recommendedAction") is not None else None),
        legalControls=[digest] * len(payload.get("legalControls", [])),
    )
    return LifecycleOperationProjection.model_validate(payload)


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
    elapsed_seconds = max(0.0, (finish - start).total_seconds())
    identity = operation_projection_identity(record)
    try:
        _validate_dependent_identities(identity, context)
        return _coherent_operation_projection(
            record,
            identity=identity,
            contract=contract,
            context=context,
            elapsed_seconds=elapsed_seconds,
        )
    except LifecycleProjectionIncoherence as error:
        return _incoherent_operation_projection(
            record,
            identity=identity,
            elapsed_seconds=elapsed_seconds,
            error=error,
        )


def _coherent_operation_projection(
    record: LifecycleOperationRecord,
    *,
    identity: LifecycleProjectionIdentity,
    contract: WorktreeContract | None,
    context: OperationProjectionContext,
    elapsed_seconds: float,
) -> LifecycleOperationProjection:
    components = _projection_components(record, contract, context)
    digest = identity.identityDigest
    return LifecycleOperationProjection(
        identity=identity,
        componentBindings=LifecycleProjectionComponentBindings(
            result=digest if components.result is not None else None,
            approval=digest,
            worker=digest,
            recommendedAction=digest if components.recommendation is not None else None,
            legalControls=[digest] * len(components.legal_controls),
        ),
        kind=record.operationKind,
        status=record.status,
        phase=record.phase,
        startedAt=record.startedAt,
        heartbeatAt=record.heartbeatAt,
        finishedAt=record.finishedAt,
        elapsedSeconds=elapsed_seconds,
        currentCommand=f"lifecycle stage: {record.phase}",
        reportPath="" if record.legacyMigration is not None else record.reportPath,
        taskIntent=(
            record.taskIntent if isinstance(record.taskIntent, TaskIntentIdentity) else None
        ),
        result=components.result,
        failure=components.failure,
        guidance=components.guidance,
        worker=components.worker,
        approval=LifecycleApprovalObservation(
            state="claimed" if record.approvalClaimed else "unclaimed"
        ),
        recommendedAction=components.recommendation,
        cancellable=_operation_cancellable(
            contract=contract,
            intent_unavailable=components.intent_unavailable,
            legal_controls=components.legal_controls,
        ),
        generation=record.generation,
        legalControls=components.legal_controls,
    )


def _projection_components(
    record: LifecycleOperationRecord,
    contract: WorktreeContract | None,
    context: OperationProjectionContext,
) -> _ProjectionComponents:
    legal_controls: list[dict[str, Any]] = []
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
        raise LifecycleProjectionIncoherence(
            {"result": "public mapping"},
            {"resultType": type(public_result).__name__},
        )
    worker = _worker_observation(record)
    validate_projection_state(
        record,
        worker_state=worker.state,
        result=public_result,
        legal_controls=legal_controls,
    )
    recommendation = _recommended_action(record, worker.state, public_result, legal_controls)
    return _ProjectionComponents(
        legal_controls=legal_controls,
        result=public_result,
        failure=projected_failure,
        guidance=projected_guidance,
        worker=worker,
        recommendation=recommendation,
        intent_unavailable=legacy_intent_blocks_recovery,
    )


def operation_projection_identity(record: LifecycleOperationRecord) -> LifecycleProjectionIdentity:
    """Bind one public envelope to its exact durable revision and admitted inputs."""

    plan_digest = canonical_sha256(
        [rule.model_dump(mode="json") for rule in record.input.gatePolicy]
    )
    values = {
        "operationKind": record.operationKind,
        "contractPath": record.contractPath,
        "generation": record.generation,
        "recordRevision": record.recordRevision,
        "candidateTupleDigest": record.fingerprint,
        "planIdentityDigest": plan_digest,
    }
    return LifecycleProjectionIdentity(
        operationKind=record.operationKind,
        contractPath=record.contractPath,
        generation=record.generation,
        recordRevision=record.recordRevision,
        candidateTupleDigest=record.fingerprint,
        planIdentityDigest=plan_digest,
        identityDigest=canonical_sha256(values),
    )


def _validate_dependent_identities(
    identity: LifecycleProjectionIdentity,
    context: OperationProjectionContext,
) -> None:
    supplied = {
        "door": context.doorIdentity,
        "result": context.resultIdentity,
        "approval": context.approvalIdentity,
        "worker": context.workerIdentity,
    }
    if context.door is not None and context.doorIdentity is None:
        raise LifecycleProjectionIncoherence(
            {"doorIdentity": identity.model_dump(mode="json")},
            {"doorIdentity": "unbound"},
        )
    for owner, observed in supplied.items():
        if observed is not None and observed != identity:
            raise LifecycleProjectionIncoherence(
                {owner: identity.model_dump(mode="json")},
                {owner: observed.model_dump(mode="json")},
            )


def _worker_observation(record: LifecycleOperationRecord) -> LifecycleWorkerObservation:
    cells = (record.workerPid, record.workerLease, record.workerProcessFingerprint)
    _require_all_or_none_worker_binding(record, cells)
    termination = record.workerTermination
    if termination is not None:
        return _termination_bound_worker_observation(cells, termination)
    if all(cell is not None for cell in cells):
        return _live_worker_observation(record, cells)
    return _released_worker_observation(record)


def _require_all_or_none_worker_binding(
    record: LifecycleOperationRecord,
    cells: tuple[object | None, object | None, object | None],
) -> None:
    """Refuse a partially-retained worker binding as an incoherent projection."""
    if any(cell is not None for cell in cells) != all(cell is not None for cell in cells):
        raise LifecycleProjectionIncoherence(
            {"workerBinding": "all-or-none"},
            {
                "workerPidPresent": record.workerPid is not None,
                "workerLeasePresent": record.workerLease is not None,
                "workerFingerprintPresent": record.workerProcessFingerprint is not None,
            },
        )


def _termination_bound_worker_observation(
    cells: tuple[object | None, object | None, object | None],
    termination: WorkerTerminationEvidence,
) -> LifecycleWorkerObservation:
    exact = (
        termination.pid,
        termination.lease,
        termination.processFingerprint,
    )
    if termination.state != "exited" and exact != cells:
        raise LifecycleProjectionIncoherence(
            {"workerTerminationIdentity": "current-worker-binding"},
            {"workerTerminationIdentity": "mismatch"},
        )
    if termination.state == "exited" and any(cell is not None for cell in cells):
        raise LifecycleProjectionIncoherence(
            {"workerBinding": "released-after-exit-proof"},
            {"workerBinding": "retained-after-exit-proof"},
        )
    state: LifecycleProjectionWorkerState = (
        "termination-requested" if termination.state == "requested" else termination.state
    )
    identity = f"{termination.pid}\0{termination.lease}\0{termination.processFingerprint}"
    return LifecycleWorkerObservation(
        state=state,
        identityRetained=termination.state != "exited",
        workerIdentitySha256=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        observedAt=termination.observedAt or termination.requestedAt,
        detail=(
            "exact worker exit is proven"
            if termination.state == "exited"
            else "exact worker termination remains in progress"
        ),
    )


def _live_worker_observation(
    record: LifecycleOperationRecord,
    cells: tuple[object | None, object | None, object | None],
) -> LifecycleWorkerObservation:
    pid, lease, fingerprint = cells
    worker_identity = f"{pid}\0{lease}\0{fingerprint}"
    return LifecycleWorkerObservation(
        state="live",
        identityRetained=True,
        workerIdentitySha256=hashlib.sha256(worker_identity.encode("utf-8")).hexdigest(),
        observedAt=record.heartbeatAt or record.startedAt or record.queuedAt,
        detail="the exact current worker authority is live",
    )


def _released_worker_observation(
    record: LifecycleOperationRecord,
) -> LifecycleWorkerObservation:
    return LifecycleWorkerObservation(
        state="exited",
        identityRetained=False,
        observedAt=record.heartbeatAt or record.finishedAt or record.queuedAt,
        detail="no live worker authority is retained",
    )


def _recommended_action(
    record: LifecycleOperationRecord,
    worker_state: LifecycleProjectionWorkerState,
    result: dict[str, Any] | None,
    legal_controls: list[dict[str, Any]],
) -> LifecycleRecommendedAction | None:
    if worker_state in {"termination-requested", "termination-required"} or (
        record.status == "termination-required"
    ):
        return _recommended_control("cancel", legal_controls)
    if result is not None and (
        result.get("developerDecisionRequired") is True
        or result.get("nextAction") == "developer-decision"
    ):
        return LifecycleRecommendedAction(
            action="developer-decision",
            summary="Resolve the bounded developer-decision evidence before mutation.",
        )
    if record.status in {"queued", "running", "completed"} and (
        worker_state == "live" or record.status != "queued"
    ):
        return LifecycleRecommendedAction(
            action="observe",
            tool="worktree_status",
            arguments={"contract_path": record.contractPath},
            summary="Observe this exact lifecycle generation for its next durable edge.",
        )
    for action in ("recover", "retry"):
        recommended = _recommended_control(action, legal_controls)
        if recommended is not None:
            return recommended
    return None


def _recommended_control(
    action: str,
    legal_controls: list[dict[str, Any]],
) -> LifecycleRecommendedAction | None:
    control = next((item for item in legal_controls if item.get("action") == action), None)
    if control is None:
        return None
    arguments = control.get("arguments")
    return LifecycleRecommendedAction(
        action=action,
        tool=str(control.get("tool")),
        arguments=dict(arguments) if isinstance(arguments, dict) else None,
        summary=str(control.get("summary") or f"Apply the exact {action} control."),
        mutating=True,
    )


def _incoherent_operation_projection(
    record: LifecycleOperationRecord,
    *,
    identity: LifecycleProjectionIdentity,
    elapsed_seconds: float,
    error: LifecycleProjectionIncoherence,
) -> LifecycleOperationProjection:
    surface = "The lifecycle observation does not bind one coherent journal revision."
    result = {
        "state": "lifecycle-projection-incoherent",
        "summary": surface,
        "nextAction": "reread",
        "expected": error.expected,
        "observed": error.observed,
    }
    return LifecycleOperationProjection(
        identity=identity,
        componentBindings=LifecycleProjectionComponentBindings(result=identity.identityDigest),
        kind=record.operationKind,
        status="incoherent",
        phase=record.phase,
        startedAt=record.startedAt,
        heartbeatAt=record.heartbeatAt,
        finishedAt=record.finishedAt,
        elapsedSeconds=elapsed_seconds,
        currentCommand=f"lifecycle stage: {record.phase}",
        reportPath="" if record.legacyMigration is not None else record.reportPath,
        taskIntent=(
            record.taskIntent if isinstance(record.taskIntent, TaskIntentIdentity) else None
        ),
        result=result,
        failure="lifecycle-projection-incoherent",
        guidance=surface,
        cancellable=False,
        generation=record.generation,
        legalControls=[],
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
    *,
    contract: WorktreeContract | None,
    intent_unavailable: bool,
    legal_controls: list[dict[str, object]],
) -> bool:
    if intent_unavailable or contract is None:
        return False
    return any(item.get("action") == "cancel" for item in legal_controls)


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
