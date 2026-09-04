"""Revision-bound public lifecycle-operation projection contracts.

CCR-R18: every public lifecycle observation is one internally valid projection of
one exact operation kind, public generation, monotonic journal revision, and
candidate/plan binding.  This module owns the versioned state matrix and the atomic
public envelope; nested result/approval/liveness/recommendation/control observations
either bind that envelope or are omitted, and an incoherent composition refuses with
a bounded typed finding instead of splicing individually-valid facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self, cast, get_args

from pydantic import Field, model_validator

from agents_remember.models.base import StrictResponseModel
from agents_remember.models.closeout.projection import TaskDocProjectionEffect
from agents_remember.models.lifecycles.operation_kinds import (
    LifecycleOperationKind,
    LifecycleOperationPhase,
    LifecycleOperationStatus,
)
from agents_remember.models.task_intent import TaskIntentIdentity

if TYPE_CHECKING:
    from agents_remember.models.lifecycles.operation import LifecycleOperationRecord

_DIGEST = r"^[0-9a-f]{64}$"

LifecycleProjectionWorkerState = Literal[
    "live",
    "termination-requested",
    "termination-required",
    "exited",
]
LifecycleProjectionResultClass = Literal[
    "none",
    "progress",
    "recovery",
    "developer-decision",
    "termination",
    "terminal",
]
LifecycleProjectionControlAction = Literal[
    "retry",
    "recover",
    "cancel",
    "revise",
    "retire",
    "supersede",
    "integrate",
    "direct-landing",
]

STATE_MATRIX_VERSION = "lifecycle-operation-state-matrix/v1"

_RUNNING_PHASES: frozenset[LifecycleOperationPhase] = frozenset(
    {
        "preflight",
        "memory-preflight",
        "quality",
        "approval-claim",
        "recovering-after-claim",
        "code-commit",
        "memory-refresh",
        "memory-commit",
        "ledger-commit",
        "integration-replay",
        "integration-quality",
        "source-merge",
        "contract-finalization",
        "door-publication",
        "direct-preflight",
        "direct-memory-commit",
        "direct-ledger-commit",
        "direct-terminal-publication",
    }
)
_DIRECT_PHASES: frozenset[LifecycleOperationPhase] = frozenset(
    {
        "direct-preflight",
        "direct-memory-commit",
        "direct-ledger-commit",
        "direct-terminal-publication",
    }
)
# input-required parks the operation where the interruption left it: the
# contract-finalization/developer-decision cells, the direct-landing decision
# cells, plus the running phases the shared evidence reporters legitimately park
# under (memory-commit / ledger-commit after a proven Git mutation, and
# recovering-after-claim for migrated legacy generations).  require_input never
# rewrites the phase, so every one of these cells is a canonical input-required
# state and must project coherently (each is already coherent under running).
_INPUT_REQUIRED_PHASES: frozenset[LifecycleOperationPhase] = frozenset(
    {
        "contract-finalization",
        "failed",
        "memory-commit",
        "ledger-commit",
        "recovering-after-claim",
        *_DIRECT_PHASES,
    }
)


@dataclass(frozen=True)
class LifecycleProjectionStateRule:
    phases: frozenset[LifecycleOperationPhase]
    workerStates: frozenset[LifecycleProjectionWorkerState]
    resultClasses: frozenset[LifecycleProjectionResultClass]
    controlActions: frozenset[LifecycleProjectionControlAction]


STATE_MATRIX: dict[LifecycleOperationStatus, LifecycleProjectionStateRule] = {
    "queued": LifecycleProjectionStateRule(
        frozenset({"queued", "recovering-after-claim"}),
        frozenset({"live", "exited"}),
        frozenset({"none", "progress", "recovery", "developer-decision"}),
        frozenset({"recover", "cancel", "revise", "supersede"}),
    ),
    "running": LifecycleProjectionStateRule(
        _RUNNING_PHASES,
        frozenset({"live", "exited"}),
        frozenset({"none", "progress", "recovery", "developer-decision"}),
        frozenset({"recover", "cancel", "revise", "supersede"}),
    ),
    "input-required": LifecycleProjectionStateRule(
        _INPUT_REQUIRED_PHASES,
        frozenset({"live", "exited"}),
        frozenset({"recovery", "developer-decision"}),
        frozenset({"retry", "recover", "cancel", "revise", "supersede"}),
    ),
    "termination-required": LifecycleProjectionStateRule(
        frozenset({"termination-required"}),
        frozenset({"termination-requested", "termination-required", "exited"}),
        frozenset({"termination"}),
        frozenset({"cancel"}),
    ),
    "completed": LifecycleProjectionStateRule(
        frozenset({"completed"}),
        frozenset({"live", "exited"}),
        frozenset({"none", "developer-decision", "terminal"}),
        frozenset({"recover", "retire", "supersede", "integrate", "direct-landing"}),
    ),
    "failed": LifecycleProjectionStateRule(
        frozenset({"failed"}),
        frozenset({"live", "exited"}),
        frozenset({"none", "recovery", "developer-decision", "terminal"}),
        frozenset({"retry", "recover", "cancel", "revise", "supersede"}),
    ),
    "cancelled": LifecycleProjectionStateRule(
        frozenset({"cancelled"}),
        frozenset({"exited"}),
        frozenset({"none", "recovery", "developer-decision", "terminal"}),
        frozenset({"cancel", "revise", "integrate", "direct-landing"}),
    ),
}


class LifecycleProjectionIncoherence(RuntimeError):
    """A bounded internal finding translated to a read-only public refusal."""

    def __init__(self, expected: dict[str, object], observed: dict[str, object]) -> None:
        self.expected = expected
        self.observed = observed
        super().__init__("lifecycle projection is incoherent")


def classify_result(
    status: LifecycleOperationStatus,
    result: dict[str, Any] | None,
) -> LifecycleProjectionResultClass:
    if result is None:
        classification: LifecycleProjectionResultClass = "none"
    elif result.get("state") == "worker-termination-required":
        classification = "termination"
    elif result.get("developerDecisionRequired") is True or result.get("nextAction") == (
        "developer-decision"
    ):
        classification = "developer-decision"
    elif status in {"completed", "failed", "cancelled"}:
        classification = "terminal"
    elif status == "input-required" or result.get("nextAction") in {
        "retry",
        "recover",
        "retire-and-republish",
    }:
        classification = "recovery"
    else:
        classification = "progress"
    return classification


def validate_projection_state(
    record: LifecycleOperationRecord,
    *,
    worker_state: LifecycleProjectionWorkerState,
    result: dict[str, Any] | None,
    legal_controls: list[dict[str, Any]],
) -> None:
    """Reject every status/phase/liveness/result/control cell not declared by v1."""

    _validate_projection_cell(
        status=record.status,
        phase=record.phase,
        worker_state=worker_state,
        result=result,
        legal_controls=legal_controls,
    )
    if record.cancelRequested and record.status not in {"termination-required", "cancelled"}:
        raise LifecycleProjectionIncoherence(
            {"cancelRequestedStatuses": ["termination-required", "cancelled"]},
            {"status": record.status, "cancelRequested": record.cancelRequested},
        )


def _validate_projection_cell(
    *,
    status: LifecycleOperationStatus,
    phase: LifecycleOperationPhase,
    worker_state: LifecycleProjectionWorkerState,
    result: dict[str, Any] | None,
    legal_controls: list[dict[str, Any]],
) -> None:
    """Validate the public subset again after projection-owned composition."""

    rule = STATE_MATRIX[status]
    result_class = classify_result(status, result)
    observed = {
        "status": status,
        "phase": phase,
        "workerState": worker_state,
        "resultClass": result_class,
        "controlActions": [str(item.get("action", "")) for item in legal_controls],
    }
    expected = {
        "stateMatrixVersion": STATE_MATRIX_VERSION,
        "phases": sorted(rule.phases),
        "workerStates": sorted(rule.workerStates),
        "resultClasses": sorted(rule.resultClasses),
        "controlActions": sorted(rule.controlActions),
    }
    if (
        phase not in rule.phases
        or worker_state not in rule.workerStates
        or result_class not in rule.resultClasses
    ):
        raise LifecycleProjectionIncoherence(expected, observed)
    actions = observed["controlActions"]
    if (
        not isinstance(actions, list)
        or len(actions) != len(set(actions))
        or any(action not in rule.controlActions for action in actions)
    ):
        raise LifecycleProjectionIncoherence(expected, observed)
    if status == "termination-required" and actions != ["cancel"]:
        raise LifecycleProjectionIncoherence({**expected, "controlActions": ["cancel"]}, observed)


def validate_state_matrix_is_exhaustive() -> None:
    """Fail if either public status or phase vocabulary changes without this matrix."""

    statuses = frozenset(get_args(LifecycleOperationStatus))
    phases = frozenset(get_args(LifecycleOperationPhase))
    worker_states = frozenset(get_args(LifecycleProjectionWorkerState))
    result_classes = frozenset(get_args(LifecycleProjectionResultClass))
    control_actions = frozenset(get_args(LifecycleProjectionControlAction))
    declared_phases = frozenset(phase for rule in STATE_MATRIX.values() for phase in rule.phases)
    declared_workers = frozenset(
        state for rule in STATE_MATRIX.values() for state in rule.workerStates
    )
    declared_results = frozenset(
        result for rule in STATE_MATRIX.values() for result in rule.resultClasses
    )
    declared_controls = frozenset(
        action for rule in STATE_MATRIX.values() for action in rule.controlActions
    )
    if (
        frozenset(STATE_MATRIX) != statuses
        or declared_phases != phases
        or declared_workers != worker_states
        or declared_results != result_classes
        or declared_controls != control_actions
    ):
        raise RuntimeError("lifecycle state matrix does not exhaust its public vocabulary")


validate_state_matrix_is_exhaustive()


class LifecycleProjectionIdentity(StrictResponseModel):
    """One exact journal snapshot and its admitted candidate/plan."""

    operationKind: LifecycleOperationKind
    contractPath: str = Field(min_length=1, max_length=4096)
    generation: int = Field(ge=1)
    recordRevision: int = Field(ge=1)
    candidateTupleDigest: str = Field(pattern=_DIGEST)
    planIdentityDigest: str = Field(pattern=_DIGEST)
    identityDigest: str = Field(pattern=_DIGEST)


class LifecycleProjectionComponentBindings(StrictResponseModel):
    """Identity digest claimed by every optional nested projection component."""

    result: str | None = Field(default=None, pattern=_DIGEST)
    approval: str | None = Field(default=None, pattern=_DIGEST)
    worker: str | None = Field(default=None, pattern=_DIGEST)
    recommendedAction: str | None = Field(default=None, pattern=_DIGEST)
    legalControls: list[str] = Field(default_factory=list, max_length=32)


class LifecycleWorkerObservation(StrictResponseModel):
    """Public liveness class without exposing process or lease authority."""

    state: Literal["live", "termination-requested", "termination-required", "exited"]
    identityRetained: bool
    workerIdentitySha256: str | None = Field(default=None, pattern=_DIGEST)
    observedAt: str | None = None
    detail: str = Field(max_length=1024)


class LifecycleApprovalObservation(StrictResponseModel):
    """Approval claim read from the same exact journal revision."""

    state: Literal["claimed", "unclaimed"]


class LifecycleRecommendedAction(StrictResponseModel):
    """Required recovery edge, distinct from optional legal controls."""

    action: str = Field(min_length=1, max_length=128)
    tool: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] | None = None
    summary: str = Field(min_length=1, max_length=2048)
    mutating: bool = False


class LifecycleOperationProjection(StrictResponseModel):
    """One coherent, task-addressed lifecycle journal observation."""

    schemaVersion: Literal["lifecycle-operation-projection/v1"] = (
        "lifecycle-operation-projection/v1"
    )
    stateMatrixVersion: Literal["lifecycle-operation-state-matrix/v1"] = (
        "lifecycle-operation-state-matrix/v1"
    )
    identity: LifecycleProjectionIdentity | None = None
    componentBindings: LifecycleProjectionComponentBindings | None = None
    kind: LifecycleOperationKind
    status: LifecycleOperationStatus | Literal["unreadable", "incoherent"]
    phase: LifecycleOperationPhase
    startedAt: str | None = None
    heartbeatAt: str | None = None
    finishedAt: str | None = None
    elapsedSeconds: float
    currentCommand: str = ""
    reportPath: str
    taskIntent: TaskIntentIdentity | None = None
    result: dict[str, Any] | None = None
    failure: str | None = None
    guidance: str | None = None
    worker: LifecycleWorkerObservation | None = None
    approval: LifecycleApprovalObservation | None = None
    recommendedAction: LifecycleRecommendedAction | None = None
    cancellable: bool = False
    generation: int | None = None
    # CCR-R15: the durable meaningful-state revision of the exact journal snapshot
    # this envelope projects. recordRevision (inside identity) advances on
    # every durable write including heartbeats; this cursor advances only when the
    # meaningful state subset changed, so status-change waiters compare it and never
    # wake on heartbeat/current-command/log growth. Adapters always populate it for
    # record-bound envelopes; unreadable journal refusals carry no record and omit it.
    meaningfulRevision: int | None = Field(default=None, ge=1)
    legalControls: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    projectionEffects: list[TaskDocProjectionEffect] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _require_component_identity(self) -> Self:
        if self.status == "unreadable":
            _require_unreadable_projection_has_no_authority(self)
            return self
        if self.identity is None or self.componentBindings is None:
            raise ValueError("readable projection requires its exact journal identity")
        _require_component_bindings_match_envelope(self)
        if self.generation != self.identity.generation or self.kind != self.identity.operationKind:
            raise ValueError("compact lifecycle identity contradicts its envelope")
        if self.status == "incoherent" and _projection_advertises_authority(self):
            raise ValueError("incoherent projection cannot advertise mutating authority")
        if self.status != "incoherent":
            _require_coherent_projection_components(self)
        return self


def _projection_advertises_authority(
    projection: LifecycleOperationProjection,
) -> bool:
    """Whether the envelope claims mutating control authority it may not hold."""
    return bool(
        projection.recommendedAction is not None
        or projection.legalControls
        or projection.cancellable
    )


def _require_unreadable_projection_has_no_authority(
    projection: LifecycleOperationProjection,
) -> None:
    """Refuse identity or control authority on an unreadable envelope."""
    if (
        projection.identity is not None
        or projection.componentBindings is not None
        or projection.recommendedAction is not None
        or projection.legalControls
        or projection.cancellable
    ):
        raise ValueError("unreadable projection cannot claim identity or control authority")


def _require_component_bindings_match_envelope(
    projection: LifecycleOperationProjection,
) -> None:
    """Bind every present component cell to the exact envelope identity digest."""
    identity = projection.identity
    component_bindings = projection.componentBindings
    if identity is None or component_bindings is None:
        raise ValueError("readable projection requires its exact journal identity")
    digest = identity.identityDigest
    expected = {
        "result": digest if projection.result is not None else None,
        "approval": digest if projection.approval is not None else None,
        "worker": digest if projection.worker is not None else None,
        "recommendedAction": digest if projection.recommendedAction is not None else None,
    }
    observed = component_bindings.model_dump(mode="python", exclude={"legalControls"})
    if observed != expected:
        raise ValueError("projection component bindings do not match their envelope")
    if component_bindings.legalControls != [digest] * len(projection.legalControls):
        raise ValueError("legal controls do not bind the projection envelope")


def _require_coherent_projection_components(
    projection: LifecycleOperationProjection,
) -> None:
    status = projection.status
    if status in {"unreadable", "incoherent"}:
        raise ValueError("coherent component validation requires a lifecycle status")
    coherent_status = cast(LifecycleOperationStatus, status)
    worker = projection.worker
    if worker is None:
        raise ValueError("coherent projection requires an explicit worker observation")
    try:
        _validate_projection_cell(
            status=coherent_status,
            phase=projection.phase,
            worker_state=worker.state,
            result=projection.result,
            legal_controls=projection.legalControls,
        )
    except LifecycleProjectionIncoherence as error:
        raise ValueError("projection components violate the state matrix") from error
    if projection.cancellable != any(
        control.get("action") == "cancel" for control in projection.legalControls
    ):
        raise ValueError("cancellable must reflect the exact legal cancel control")
    _require_recommendation_coherence(projection)
    _require_projection_task_addresses(projection)


def _require_recommendation_coherence(projection: LifecycleOperationProjection) -> None:
    recommendation = projection.recommendedAction
    worker = projection.worker
    if recommendation is None or worker is None:
        return
    if (
        recommendation.action == "cancel"
        and worker.state == "live"
        and projection.status in {"queued", "running"}
    ):
        raise ValueError("healthy live work cannot receive required-cancel guidance")
    matching_controls = [
        control
        for control in projection.legalControls
        if control.get("action") == recommendation.action
    ]
    if not matching_controls:
        return
    if len(matching_controls) != 1 or not recommendation.mutating:
        raise ValueError("recommended control does not match one exact legal control")
    control = matching_controls[0]
    arguments = control.get("arguments")
    expected_arguments = dict(arguments) if isinstance(arguments, dict) else None
    if recommendation.tool != control.get("tool") or recommendation.arguments != expected_arguments:
        raise ValueError("recommended control does not match one exact legal control")


def _require_projection_task_addresses(projection: LifecycleOperationProjection) -> None:
    identity = projection.identity
    if identity is None:
        raise ValueError("task-address validation requires a projection identity")
    arguments = [
        *(item.get("arguments") for item in projection.legalControls),
        *(
            [projection.recommendedAction.arguments]
            if projection.recommendedAction is not None
            else []
        ),
    ]
    for value in arguments:
        if not isinstance(value, dict):
            continue
        observed = {
            str(value[field])
            for field in ("contract_path", "enclosure_path", "contractPath", "enclosurePath")
            if field in value
        }
        if observed and observed != {identity.contractPath}:
            raise ValueError("projection guidance names a different task address")
