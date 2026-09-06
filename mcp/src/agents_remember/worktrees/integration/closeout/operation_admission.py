"""Lease-owned normalization and immutable admission for closeout operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agents_remember.models.certification.corrective import RedCatalogDisposition
from agents_remember.models.closeout.input import (
    CloseoutCorrectedCall,
    CloseoutMessageInput,
    EffectiveCloseoutInput,
    ResolvedCloseoutPlan,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    GatePolicyRuleSnapshot,
    LifecycleOperationRecord,
)
from agents_remember.models.task_intent import (
    TaskIntentIdentity,
    task_intent_is_missing,
)
from agents_remember.tasks.task_intent import require_current_task_intent
from agents_remember.worktrees.closeout_input import (
    CloseoutCandidateSnapshot,
    candidate_drift_error,
    capture_closeout_candidate,
    normalize_closeout_input,
    resolve_closeout_plan,
    resolved_plan_from_effective_input,
)
from agents_remember.worktrees.integration.closeout.door import classify_door_publication
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
)
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    current_door_task_intent,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
    LifecycleOperationCandidateBinding,
    lifecycle_operation_candidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    reconcile_closeout_mutations,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract


@dataclass(frozen=True)
class CloseoutOperationAdmission:
    """Raw, non-authoritative closeout request resolved only inside the lifecycle lease."""

    config_path: str
    contract_path: Path
    messages: CloseoutMessageInput
    approval_note: str
    gate_policy: list[GatePolicyRuleSnapshot]
    corrected_call: CloseoutCorrectedCall
    corrective_dispositions: tuple[RedCatalogDisposition, ...] = ()


@dataclass(frozen=True)
class CloseoutAdmissionSnapshot:
    state: str
    candidate: CloseoutCandidateSnapshot


@dataclass(frozen=True)
class ValidatedCloseoutAdmission:
    """Current-state input validation completed before lifecycle records are observed."""

    operation_input: CloseoutOperationInput
    snapshot: CloseoutAdmissionSnapshot
    candidate: LifecycleOperationCandidate


def prevalidate_closeout_operation_admission(
    contract: WorktreeContract,
    admission: CloseoutOperationAdmission,
) -> ValidatedCloseoutAdmission:
    """Normalize raw input against one stable current candidate before authority decisions."""

    snapshot = capture_closeout_admission_snapshot(contract)
    plan = resolve_closeout_plan(
        contract,
        route="worktree",
        candidate=snapshot.candidate,
    )
    effective = normalize_closeout_input(
        contract,
        admission.messages,
        route="worktree",
        corrected_call=admission.corrected_call,
        resolved_plan=plan,
    )
    _require_stable_snapshot(contract, snapshot, plan, admission.corrected_call)
    operation_input = _operation_input(contract, admission, effective)
    intent = current_door_task_intent(contract)
    return ValidatedCloseoutAdmission(
        operation_input=operation_input,
        snapshot=snapshot,
        candidate=lifecycle_operation_candidate(
            LifecycleOperationCandidateBinding(
                operation_input=operation_input,
                candidate_state=snapshot.state,
                closeout_candidate=snapshot.candidate,
                closeout_door_generation_id=_current_door_generation_id(contract),
                task_intent=intent,
            )
        ),
    )


def resolve_closeout_operation_admission(
    contract: WorktreeContract,
    current: LifecycleOperationRecord | None,
    admission: CloseoutOperationAdmission,
    validated: ValidatedCloseoutAdmission,
) -> tuple[CloseoutOperationInput, LifecycleOperationCandidate]:
    """Resolve one new or duplicate request against a stable accepted generation."""
    if current is not None and not isinstance(current.input, CloseoutOperationInput):
        raise RuntimeError("closeout journal contains a non-closeout durable input")
    if (
        current is None
        or current.status == "cancelled"
        or (
            task_intent_is_missing(current.taskIntent)
            and current.status in {"completed", "failed", "cancelled"}
        )
        or _completed_generation_was_advanced(
            contract,
            current,
            validated.snapshot.candidate,
        )
    ):
        # Cancellation terminally preserves the old generation and publishes a distinct
        # waiting door successor.  Its fresh input/candidate must reach the replacement
        # transaction, where the claimed-predecessor edge, cancellation evidence, and
        # worker-exit proof are checked together; treating it as an old-generation retry
        # makes every valid successor impossible by construction.
        return validated.operation_input, validated.candidate
    return _validate_existing_closeout_request(contract, current, admission, validated)


def capture_closeout_admission_snapshot(
    contract: WorktreeContract,
) -> CloseoutAdmissionSnapshot:
    return CloseoutAdmissionSnapshot(
        state=closeout_contract_sha256(contract),
        candidate=capture_closeout_candidate(contract),
    )


def _validate_existing_closeout_request(
    contract: WorktreeContract,
    current: LifecycleOperationRecord,
    admission: CloseoutOperationAdmission,
    validated: ValidatedCloseoutAdmission,
) -> tuple[CloseoutOperationInput, LifecycleOperationCandidate]:
    accepted = current.input
    assert isinstance(accepted, CloseoutOperationInput)
    plan = resolved_plan_from_effective_input(accepted.effectiveInput)
    retained = closeout_generation_retained(current)
    effective = normalize_closeout_input(
        contract,
        admission.messages,
        route="worktree",
        corrected_call=admission.corrected_call,
        resolved_plan=plan,
    )
    operation_input = _operation_input(contract, admission, effective)
    if operation_input != accepted:
        raise RuntimeError(
            "conflicting closeout intent targets an existing accepted generation; "
            "observe or recover it with the exact accepted input"
        )
    door_publication = current.doorPublication
    door_state_is_accepted = bool(
        door_publication is not None
        and classify_door_publication(door_publication, contract).state == "published"
    )
    if retained or door_state_is_accepted:
        intent = _current_operation_task_intent(current, validated.candidate.task_intent)
        _require_recovery_identity(contract, current)
        return operation_input, LifecycleOperationCandidate(
            current.candidateState,
            current.candidateTree,
            current.fingerprint,
            intent,
        )
    return operation_input, lifecycle_operation_candidate(
        LifecycleOperationCandidateBinding(
            operation_input=operation_input,
            candidate_state=validated.snapshot.state,
            closeout_candidate=validated.snapshot.candidate,
            closeout_door_generation_id=_current_door_generation_id(contract),
            task_intent=validated.candidate.task_intent,
        )
    )


def _completed_generation_was_advanced(
    contract: WorktreeContract,
    current: LifecycleOperationRecord,
    candidate: CloseoutCandidateSnapshot,
) -> bool:
    """Permit a sequential generation only after exact prior finalization advanced again."""

    return (
        current.status == "completed"
        and closeout_generation_retained(current)
        and (
            current.closeoutFinalizedContractSha256 is None
            or closeout_contract_sha256(contract) != current.closeoutFinalizedContractSha256
            or not _candidate_is_generation_output(current, candidate)
        )
    )


def _operation_input(
    contract: WorktreeContract,
    admission: CloseoutOperationAdmission,
    effective: EffectiveCloseoutInput,
) -> CloseoutOperationInput:
    return CloseoutOperationInput(
        configPath=admission.config_path,
        contractPath=contract.contract_path.as_posix(),
        effectiveInput=effective,
        approvalNote=admission.approval_note,
        gatePolicy=admission.gate_policy,
        correctiveDispositions=admission.corrective_dispositions,
    )


def _require_stable_snapshot(
    contract: WorktreeContract,
    before: CloseoutAdmissionSnapshot,
    plan: ResolvedCloseoutPlan,
    corrected_call: CloseoutCorrectedCall,
) -> None:
    after_contract = load_contract(contract.contract_path)
    after = capture_closeout_admission_snapshot(after_contract)
    if after_contract != contract or after != before:
        raise candidate_drift_error(plan, corrected_call=corrected_call)


def _require_recovery_identity(
    contract: WorktreeContract,
    current: LifecycleOperationRecord,
) -> None:
    contract_state = closeout_contract_sha256(contract)
    accepted_states = {current.candidateState}
    if current.closeoutFinalizedContractSha256 is not None:
        accepted_states.add(current.closeoutFinalizedContractSha256)
    if current.doorPublication is not None:
        accepted_states.add(current.doorPublication.expectedPublishedContractSha256)
    if contract_state not in accepted_states:
        raise RuntimeError(
            "closeout contract identity changed outside the accepted generation's proven output"
        )
    candidate = capture_closeout_candidate(contract)
    if _candidate_is_generation_output(current, candidate):
        return
    raise RuntimeError("closeout candidate changed outside the accepted generation's proven output")


def _candidate_is_generation_output(
    current: LifecycleOperationRecord,
    candidate: CloseoutCandidateSnapshot,
) -> bool:
    if candidate.candidate_tree != current.candidateTree:
        return False
    accepted = current.input
    assert isinstance(accepted, CloseoutOperationInput)
    unchanged = lifecycle_operation_candidate(
        LifecycleOperationCandidateBinding(
            operation_input=accepted,
            candidate_state=current.candidateState,
            closeout_candidate=candidate,
            closeout_door_generation_id=(
                current.doorPublication.generation.generationId
                if current.doorPublication is not None
                else None
            ),
            task_intent=(
                current.taskIntent if isinstance(current.taskIntent, TaskIntentIdentity) else None
            ),
        )
    )
    if unchanged.fingerprint == current.fingerprint:
        return True
    code = reconcile_closeout_mutations(current).get("code")
    return bool(
        code is not None
        and code.state == "commit-proven"
        and code.commit == candidate.head_commit
        and code.expectedOutputTree == candidate.head_tree
    )


def _current_door_generation_id(contract: WorktreeContract) -> str:
    door = contract.closeout_door
    if door is None:
        raise RuntimeError("closeout admission requires one exact door generation")
    return door.generationId


def _current_operation_task_intent(
    current: LifecycleOperationRecord,
    expected: TaskIntentIdentity | None,
) -> TaskIntentIdentity:
    if expected is None:
        raise RuntimeError("commit operation candidate is missing canonical task intent")
    return require_current_task_intent(
        current.taskIntent,
        expected,
        owner="lifecycle-operation",
        next_action="retire-and-republish",
    )
