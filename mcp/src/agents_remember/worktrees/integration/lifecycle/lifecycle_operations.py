"""Task-addressed start, observe, recover, cancel, and projection for lifecycle jobs."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.git_command import git_environment
from agents_remember.kernel.platform_subprocess import (
    native_command,
    native_subprocess_environment,
)
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    IntegrateOperationInput,
    IntegrationConflictTransaction,
    IntegrationOperationAuthority,
    LifecycleOperationInput,
    LifecycleOperationKind,
    LifecycleOperationProjection,
    LifecycleOperationRecord,
    lifecycle_operation_dependencies,
    require_lifecycle_operation_dependencies,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    TaskIntentIdentity,
    task_intent_is_missing,
)
from agents_remember.worktrees.closeout_input import require_effective_closeout_plan
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationError,
    classify_door_publication,
    door_generation_for_operation,
    prepare_door_publication,
    publish_door_intent,
)
from agents_remember.worktrees.integration.closeout.operation_admission import (
    CloseoutOperationAdmission,
    prevalidate_closeout_operation_admission,
    resolve_closeout_operation_admission,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.configured_contract_authority import (
    reread_configured_contract,
)
from agents_remember.worktrees.integration.integration_branch_authority import integration_targets
from agents_remember.worktrees.integration.integration_publication_fence import (
    classify_integration_door_authority,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_closeout_claim_evidence import (
    closeout_preview_args,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_generation_resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
    LifecycleOperationCandidateBinding,
    fingerprint_payload,
    lifecycle_operation_candidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    contract_lifecycle_lease,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    located_lifecycle_operation_report_path,
    located_lifecycle_operation_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    OperationProjectionContext,
    operation_projection,
    parse_operation_stamp,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.worker import launch as lifecycle_worker_launch
from agents_remember.worktrees.integration.lifecycle.worker.child_processes import (
    retain_detached_worker_child,
)
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    require_linux_worker_runtime,
    worker_process_fingerprint,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    initial_closeout_mutation_evidence,
    reconcile_closeout_mutations,
)
from agents_remember.worktrees.modules.git import branch_commit, is_ancestor
from agents_remember.worktrees.queue.closeout_projection_publication import (
    projection_refresh_failure_effect,
    refresh_closeout_projection,
)
from agents_remember.worktrees.queue.closeout_queue import require_first_ready_generation
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    load_contract,
)

STALE_HEARTBEAT_SECONDS = 30.0
OperationLauncher = Callable[[WorktreeContract, LifecycleOperationRecord], None]


@dataclass(frozen=True)
class _OperationExecution:
    timestamp: datetime
    launcher: OperationLauncher


@dataclass(frozen=True)
class _CloseoutClaimContext:
    contract: WorktreeContract
    store: LifecycleOperationStore
    operation_input: CloseoutOperationInput
    candidate: LifecycleOperationCandidate
    queued: LifecycleOperationRecord
    door: CloseoutDoorGeneration


@dataclass(frozen=True)
class _GenerationReplacement:
    store: LifecycleOperationStore
    queued: LifecycleOperationRecord
    current: LifecycleOperationRecord
    contract: WorktreeContract
    operation_input: LifecycleOperationInput
    candidate: LifecycleOperationCandidate


class _CloseoutResumeNoLongerRequired(Exception):
    """The current record advanced while an exact duplicate was being resumed."""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def operation_fingerprint(operation_input: LifecycleOperationInput) -> str:
    return fingerprint_payload(operation_input.model_dump(mode="json"))


def operation_key(contract_path: Path, kind: LifecycleOperationKind, fingerprint: str) -> str:
    identity = f"{contract_path.resolve().as_posix()}\0{kind}\0{fingerprint}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def start_or_observe_operation(
    operation_input: LifecycleOperationInput,
    admitted_contract: WorktreeContract,
    *,
    launcher: OperationLauncher | None = None,
    now: datetime | None = None,
) -> LifecycleOperationProjection:
    if not isinstance(operation_input, IntegrateOperationInput):
        raise RuntimeError(
            "closeout operations require lease-bound raw-input admission through "
            "start_or_observe_closeout_operation"
        )
    with contract_lifecycle_lease(admitted_contract):
        contract, _location = reread_configured_contract(
            admitted_contract,
            operation_input.configPath,
        )
        _validate_input_identity(contract, operation_input)
        door_authority = classify_integration_door_authority(contract, None)
        if not door_authority.valid:
            raise LifecycleControlError(
                door_authority.status,
                door_authority.detail,
                expected=door_authority.expected,
                observed=door_authority.observed,
                next_action="developer-decision",
            )
        require_lifecycle_operation_compatible(
            contract,
            operation_kind=operation_input.kind,
        )
        store = _store(contract, "integrate")
        retained = _retained_integration_recovery_record(store.read(), operation_input)
        if retained is None:
            integration_authority = _integration_authority(contract, operation_input)
            candidate = lifecycle_operation_candidate(
                LifecycleOperationCandidateBinding(
                    operation_input=operation_input,
                    candidate_state=operation_state_fingerprint(contract),
                    integration_authority=integration_authority,
                )
            )
        else:
            integration_authority = retained.integrationAuthority
            candidate = LifecycleOperationCandidate(
                retained.candidateState,
                retained.candidateTree,
                retained.fingerprint,
            )
        return _start_or_observe_operation(
            contract,
            operation_input,
            candidate=candidate,
            integration_authority=integration_authority,
            execution=_operation_execution(launcher, now),
        )


def start_or_observe_closeout_operation(
    admission: CloseoutOperationAdmission,
    admitted_contract: WorktreeContract,
    *,
    launcher: OperationLauncher | None = None,
    now: datetime | None = None,
) -> LifecycleOperationProjection:
    """Normalize and admit one closeout generation under its lifecycle lease."""
    with contract_lifecycle_lease(admitted_contract):
        current_contract, _location = reread_configured_contract(
            admitted_contract,
            admission.config_path,
        )
        validated = prevalidate_closeout_operation_admission(current_contract, admission)
        _validate_input_identity(current_contract, validated.operation_input)
        store = _store(current_contract, "closeout")
        _require_pending_initial_door_convergent(current_contract, store.read())
        operation_input, candidate = resolve_closeout_operation_admission(
            current_contract,
            store.read(),
            admission,
            validated,
        )
        require_lifecycle_operation_compatible(
            current_contract,
            operation_kind="closeout",
        )
        return _start_or_observe_operation(
            current_contract,
            operation_input,
            candidate=candidate,
            integration_authority=None,
            execution=_operation_execution(launcher, now),
        )


def _require_pending_initial_door_convergent(
    contract: WorktreeContract,
    record: LifecycleOperationRecord | None,
) -> None:
    if (
        record is None
        or record.operationKind != "closeout"
        or record.status not in {"queued", "running"}
        or record.doorPublication is None
        or record.doorPublication.state != "intent"
    ):
        return
    classification = classify_door_publication(record.doorPublication, contract)
    if classification.state != "developer-decision":
        return
    payload = classification.decision_payload()
    raise LifecycleControlError(
        str(payload["state"]),
        str(payload["decisionSurface"]),
        expected=classification.expected,
        observed=classification.observed,
        next_action="developer-decision",
    )


def _start_or_observe_operation(
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
    *,
    candidate: LifecycleOperationCandidate,
    integration_authority: IntegrationOperationAuthority | None,
    execution: _OperationExecution,
) -> LifecycleOperationProjection:
    store = _store(contract, operation_input.kind)
    timestamp = execution.timestamp
    if operation_input.kind == "closeout":
        _reconcile_closeout_store(store, now=timestamp, fresh_dead_worker=False)
        assert isinstance(operation_input, CloseoutOperationInput)
        current, contract, created, sprint_ref, refresh_required = _claim_closeout_operation(
            contract,
            store,
            operation_input,
            candidate=candidate,
            timestamp=timestamp,
        )
        projection_effects = []
        if refresh_required:
            try:
                projection_effects.append(
                    refresh_closeout_projection(
                        contract.coordination_root,
                        sprint_ref,
                    )
                )
            except Exception as exc:
                projection_effects.append(
                    projection_refresh_failure_effect(
                        contract.coordination_root,
                        sprint_ref,
                        exc,
                    )
                )
        projection = _recover_launch_and_project(
            contract,
            store,
            current,
            created=created,
            execution=execution,
        )
        return projection.model_copy(update={"projectionEffects": projection_effects})
    queued = queued_operation_record(
        contract,
        operation_input,
        candidate,
        integration_authority,
        timestamp,
    )
    current, created = _create_or_replace_generation(
        store,
        queued,
        contract=contract,
        operation_input=operation_input,
        candidate=candidate,
    )
    return _recover_launch_and_project(
        contract,
        store,
        current,
        created=created,
        execution=execution,
    )


def _claim_closeout_operation(
    admitted_contract: WorktreeContract,
    store: LifecycleOperationStore,
    operation_input: CloseoutOperationInput,
    *,
    candidate: LifecycleOperationCandidate,
    timestamp: datetime,
) -> tuple[LifecycleOperationRecord, WorktreeContract, bool, TaskDocumentRef, bool]:
    """Transfer one first-ready waiting generation into root-journal authority."""

    with task_publication_lock(admitted_contract.coordination_root, admitted_contract.repo_name):
        contract, _location = reread_configured_contract(
            admitted_contract,
            operation_input.configPath,
        )
        _validate_input_identity(contract, operation_input)
        queued = queued_operation_record(
            contract,
            operation_input,
            candidate,
            None,
            timestamp,
        )
        door = contract.closeout_door
        if door is None:
            raise LifecycleControlError(
                "closeout-door-missing",
                "closeout claim requires one current waiting door generation",
                expected={"disposition": "waiting"},
                observed={"disposition": "absent"},
                next_action="developer-decision",
            )
        sprint_ref = door.sprintTaskDocumentRef
        queued = _prepare_closeout_claim(
            _CloseoutClaimContext(
                contract=contract,
                store=store,
                operation_input=operation_input,
                candidate=candidate,
                queued=queued,
                door=door,
            )
        )

        current, created = _create_or_replace_generation(
            store,
            queued,
            contract=contract,
            operation_input=operation_input,
            candidate=candidate,
        )
        if not created:
            current, created = _resume_exact_duplicate_closeout(
                store,
                current,
                operation_input=operation_input,
                candidate=candidate,
            )
        if current.status not in {"queued", "running"}:
            return current, contract, created, sprint_ref, False
        publication = current.doorPublication
        refresh_required = publication is not None and publication.state == "intent"
        current, contract = _publish_initial_closeout_door(contract, store, current)
        return current, contract, created, sprint_ref, refresh_required


def _prepare_closeout_claim(
    context: _CloseoutClaimContext,
) -> LifecycleOperationRecord:
    """Validate the door owner and add the waiting-to-claimed publication intent."""

    contract = context.contract
    door = context.door
    if door.disposition == "waiting":
        _require_waiting_closeout_candidate(
            contract,
            context.operation_input,
            context.candidate,
            door,
        )
        require_first_ready_generation(
            contract.coordination_root,
            sprint_ref=door.sprintTaskDocumentRef,
            generation_id=door.generationId,
        )
        claimed = door_generation_for_operation(contract, context.queued, "claimed")
        claimed_record = context.queued.model_copy(
            update={"doorPublication": prepare_door_publication(contract, claimed)}
        )
        return claimed_record.model_copy(
            update={"dependencies": lifecycle_operation_dependencies(claimed_record)}
        )
    if door.disposition == "claimed":
        _require_retained_closeout_owner(context.store, door, context.candidate)
        return context.queued
    raise LifecycleControlError(
        "closeout-door-not-waiting",
        "closeout claim requires a waiting door generation",
        expected={"disposition": "waiting"},
        observed={"disposition": door.disposition},
        next_action="developer-decision",
    )


def _require_waiting_closeout_candidate(
    contract: WorktreeContract,
    operation_input: CloseoutOperationInput,
    candidate: LifecycleOperationCandidate,
    door: CloseoutDoorGeneration,
) -> None:
    observed_state = closeout_contract_sha256(contract)
    if candidate.state != observed_state:
        raise LifecycleControlError(
            "closeout-candidate-state-moved",
            "closeout contract bytes changed after operation admission",
            expected={"candidateState": candidate.state},
            observed={"candidateState": observed_state},
            next_action="retry-closeout-preview",
            next_tool="worktree_closeout_preview",
            next_args=closeout_preview_args(operation_input),
        )
    if candidate.tree != door.candidateTree:
        raise LifecycleControlError(
            "closeout-door-candidate-moved",
            "the admitted Git candidate no longer equals the waiting door candidate",
            expected={"candidateTree": door.candidateTree},
            observed={"candidateTree": candidate.tree or ""},
            next_action="retry-closeout-preview",
            next_tool="worktree_closeout_preview",
            next_args=closeout_preview_args(operation_input),
        )
    if not isinstance(door.taskIntent, TaskIntentIdentity):
        raise LifecycleControlError(
            "closeout-door-task-intent-unavailable",
            "the waiting door predates canonical task intent",
            next_action="closeout_door.update-provenance",
        )
    if candidate.task_intent != door.taskIntent:
        raise LifecycleControlError(
            "closeout-door-task-intent-stale",
            "the admitted task intent no longer equals the waiting door generation",
            expected={"taskIntent": door.taskIntent.model_dump(mode="json", by_alias=True)},
            observed={
                "taskIntent": (
                    candidate.task_intent.model_dump(mode="json", by_alias=True)
                    if candidate.task_intent is not None
                    else None
                )
            },
            next_action="retry-closeout-preview",
        )


def _require_retained_closeout_owner(
    store: LifecycleOperationStore,
    door: CloseoutDoorGeneration,
    candidate: LifecycleOperationCandidate,
) -> None:
    existing = store.read()
    if (
        existing is not None
        and existing.operationKind == "closeout"
        and existing.fingerprint == door.operationFingerprint
        and existing.operationKey == door.claimedOperationKey
        and existing.fingerprint == candidate.fingerprint
        and isinstance(existing.taskIntent, TaskIntentIdentity)
        and existing.taskIntent == candidate.task_intent
    ):
        return
    raise LifecycleControlError(
        "closeout-door-claim-owner-conflict",
        "the claimed door does not match the exact retained root-journal owner",
        expected={
            "operationKind": door.operationKind,
            "operationFingerprint": door.operationFingerprint,
            "operationKey": door.claimedOperationKey,
        },
        observed={
            "operationKind": existing.operationKind if existing is not None else "",
            "operationFingerprint": existing.fingerprint if existing is not None else "",
            "operationKey": existing.operationKey if existing is not None else "",
        },
        next_action="developer-decision",
    )


def _publish_initial_closeout_door(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
) -> tuple[LifecycleOperationRecord, WorktreeContract]:
    """Publish/prove the claimed door before a closeout worker can execute."""

    publication = record.doorPublication
    if publication is None:
        raise LifecycleControlError(
            "closeout-initial-door-intent-missing",
            "the canonical schema-3 closeout journal is missing its create-time claimed-door "
            "intent; normal recovery cannot synthesize lifecycle authority",
            expected={"doorPublication": "create-time-intent-or-proof"},
            observed={"doorPublication": "absent", "generation": record.generation},
            next_action="developer-decision",
        )
    if publication.generation.disposition != "claimed":
        raise LifecycleControlError(
            "closeout-door-publication-conflict",
            "active closeout generation does not own a claimed door intent",
            expected={"disposition": "claimed"},
            observed={"disposition": publication.generation.disposition},
            next_action="developer-decision",
        )
    if publication.state == "intent":
        try:
            proof = publish_door_intent(contract.contract_path, publication)
        except DoorPublicationError as exc:
            classification = exc.classification
            recoverable = classification.state == "accepted-before"
            raise LifecycleControlError(
                exc.status,
                exc.detail,
                expected=classification.expected,
                observed=classification.observed,
                next_action="recover" if recoverable else "developer-decision",
                next_tool="worktree_operation_control" if recoverable else None,
                next_args=(
                    {
                        "contract_path": contract.contract_path.as_posix(),
                        "operation_kind": "closeout",
                        "action": "recover",
                        "expected_generation": record.generation,
                        "intent_note": "<developer intent>",
                        "dry_run": False,
                    }
                    if recoverable
                    else None
                ),
            ) from exc
        record = store.update(lambda current: current.model_copy(update={"doorPublication": proof}))
        contract = load_contract(contract.contract_path)
    return record, contract


def _create_or_replace_generation(
    store: LifecycleOperationStore,
    queued: LifecycleOperationRecord,
    *,
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
    candidate: LifecycleOperationCandidate,
) -> tuple[LifecycleOperationRecord, bool]:
    """Create one generation or replace only a terminal, advanced generation."""

    current, created = store.create(queued)
    if task_intent_is_missing(current.taskIntent) and current.operationKind in {
        "closeout",
        "direct-landing",
    }:
        if current.status not in {"completed", "failed", "cancelled"}:
            raise LifecycleControlError(
                "lifecycle-operation-task-intent-unavailable",
                "the active legacy operation predates canonical task intent and cannot be reused",
                next_action="developer-decision",
            )
        return store.replace_terminal(queued), True
    if current.fingerprint == candidate.fingerprint:
        return current, created
    _require_terminal_generation(current, operation_input, contract)
    return _replace_terminal_generation(
        _GenerationReplacement(
            store=store,
            queued=queued,
            current=current,
            contract=contract,
            operation_input=operation_input,
            candidate=candidate,
        )
    )


def _require_terminal_generation(
    current: LifecycleOperationRecord,
    operation_input: LifecycleOperationInput,
    contract: WorktreeContract,
) -> None:
    if current.status not in {"completed", "failed", "cancelled"}:
        raise RuntimeError(
            f"conflicting {operation_input.kind} operation already exists for task "
            f"{contract.task_name}; wait for or resolve that task-bound operation"
        )


def _replace_terminal_generation(
    replacement: _GenerationReplacement,
) -> tuple[LifecycleOperationRecord, bool]:
    if replacement.current.status == "cancelled":
        return _replace_cancelled_generation(replacement)
    if replacement.current.status == "failed":
        raise RuntimeError(
            f"terminal {replacement.operation_input.kind} generation requires the explicit "
            "task-addressed retry/recover/revise control"
        )
    return _replace_completed_generation(replacement)


def _replace_cancelled_generation(
    replacement: _GenerationReplacement,
) -> tuple[LifecycleOperationRecord, bool]:
    if replacement.operation_input.kind == "integrate":
        return _replace_cancelled_integrate(
            replacement.store,
            replacement.queued,
            replacement.current,
            replacement.candidate,
        )
    if replacement.operation_input.kind == "closeout":
        return _replace_cancelled_closeout(
            replacement.store,
            replacement.queued,
            replacement.current,
            replacement.contract,
        )
    raise RuntimeError(
        f"terminal {replacement.operation_input.kind} generation requires the explicit "
        "task-addressed retry/recover/revise control"
    )


def _replace_cancelled_integrate(
    store: LifecycleOperationStore,
    queued: LifecycleOperationRecord,
    current: LifecycleOperationRecord,
    candidate: LifecycleOperationCandidate,
) -> tuple[LifecycleOperationRecord, bool]:
    if current.candidateState == candidate.state:
        raise RuntimeError(
            "cancelled integrate generation requires an advanced task state before "
            "a fresh integration successor"
        )
    return store.replace_terminal(queued), True


def _replace_cancelled_closeout(
    store: LifecycleOperationStore,
    queued: LifecycleOperationRecord,
    current: LifecycleOperationRecord,
    contract: WorktreeContract,
) -> tuple[LifecycleOperationRecord, bool]:
    successor = contract.closeout_door
    observed = (
        getattr(successor, "disposition", None),
        current.generationDisposition,
        getattr(current.cancellationEvidence, "workerExitProven", False),
    )
    if observed != ("waiting", "cancelled", True):
        raise RuntimeError(
            "cancelled closeout can advance only through the current waiting door "
            "after proven worker exit"
        )
    return store.replace_terminal(queued), True


def _replace_completed_generation(
    replacement: _GenerationReplacement,
) -> tuple[LifecycleOperationRecord, bool]:
    _require_released_closeout_output(
        replacement.current,
        replacement.contract,
        replacement.operation_input,
    )
    _require_advanced_integration_state(
        replacement.current,
        replacement.contract,
        replacement.operation_input,
        replacement.candidate,
    )
    return replacement.store.replace_terminal(replacement.queued), True


def _require_released_closeout_output(
    current: LifecycleOperationRecord,
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
) -> None:
    retained = all(
        (
            operation_input.kind == "closeout",
            current.generationDisposition == "active",
            contract.integration_status != "completed",
            closeout_generation_retained(current),
        )
    )
    if retained:
        raise RuntimeError(
            "completed closeout generation still owns unintegrated output; choose the "
            "advertised integrate, retire, or supersede disposition before a successor"
        )


def _require_advanced_integration_state(
    current: LifecycleOperationRecord,
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
    candidate: LifecycleOperationCandidate,
) -> None:
    unchanged = (
        operation_input.kind,
        current.status,
        current.candidateState,
    ) == ("integrate", "completed", candidate.state)
    if unchanged:
        raise RuntimeError(
            f"conflicting {operation_input.kind} parameters target an already completed "
            f"task state for {contract.task_name}; the task state has not advanced"
        )


def _resume_exact_duplicate_closeout(
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    operation_input: CloseoutOperationInput,
    candidate: LifecycleOperationCandidate,
) -> tuple[LifecycleOperationRecord, bool]:
    """Resume only the exact accepted closeout generation proven by admission."""

    if not _exact_duplicate_closeout_requires_resume(
        record,
        operation_input=operation_input,
        candidate=candidate,
    ):
        return record, False

    def resume(current: LifecycleOperationRecord) -> LifecycleOperationRecord:
        if not _exact_duplicate_closeout_requires_resume(
            current,
            operation_input=operation_input,
            candidate=candidate,
        ):
            raise _CloseoutResumeNoLongerRequired
        return requeued_same_generation(current)

    try:
        return store.resume_generation(resume, expected_generation=record.generation)
    except _CloseoutResumeNoLongerRequired:
        return store.observe_current() or record, False


def _exact_duplicate_closeout_requires_resume(
    record: LifecycleOperationRecord,
    *,
    operation_input: CloseoutOperationInput,
    candidate: LifecycleOperationCandidate,
) -> bool:
    """Classify the two mechanically resumable duplicate-apply states."""

    result = record.result if isinstance(record.result, dict) else {}
    exact_identity = (
        record.operationKind == "closeout"
        and record.input == operation_input
        and record.candidateState == candidate.state
        and record.candidateTree == candidate.tree
        and isinstance(record.taskIntent, TaskIntentIdentity)
        and record.taskIntent == candidate.task_intent
        and record.fingerprint == candidate.fingerprint
    )
    worker_authority_released = (
        record.workerPid is None
        and record.workerLease is None
        and record.workerProcessFingerprint is None
        and (record.workerTermination is None or record.workerTermination.state == "exited")
    )
    retained_output = closeout_generation_retained(record)
    resumable_status = (record.status == "failed" and not retained_output) or (
        record.status == "input-required" and retained_output
    )
    return bool(
        exact_identity
        and worker_authority_released
        and resumable_status
        and not record.cancelRequested
        and result.get("developerDecisionRequired") is not True
    )


def _recover_launch_and_project(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    current: LifecycleOperationRecord,
    *,
    created: bool,
    execution: _OperationExecution,
) -> LifecycleOperationProjection:
    """Recover an existing generation if needed, launch once, and project it."""

    timestamp = execution.timestamp
    should_launch = created
    if should_launch:
        require_lifecycle_operation_dependencies(current)
        lifecycle_worker_launch.launch_or_fail(contract, current, execution.launcher, store)
        current = store.read() or current
    return operation_projection(
        current,
        contract=contract,
        context=OperationProjectionContext(now=timestamp),
    )


def _operation_execution(
    launcher: OperationLauncher | None,
    now: datetime | None,
) -> _OperationExecution:
    return _OperationExecution(
        timestamp=(now or datetime.now(UTC)).replace(microsecond=0),
        launcher=launcher or launch_detached_worker,
    )


def _retained_integration_recovery_record(
    current: LifecycleOperationRecord | None,
    operation_input: IntegrateOperationInput,
) -> LifecycleOperationRecord | None:
    """Keep one accepted integration identity after its irreversible boundary."""

    if current is None or current.input != operation_input:
        return None
    if not current.irreversibleBoundaryEntered:
        return None
    if current.status in {"queued", "running", "input-required", "completed"}:
        return current
    return None


def _reconcile_closeout_store(
    store: LifecycleOperationStore,
    *,
    now: datetime,
    fresh_dead_worker: bool,
) -> None:
    current = store.observe_current()
    for _attempt in range(3):
        if current is None or current.operationKind != "closeout":
            return
        if current.status in {"queued", "running"}:
            stale = _recoverable_stale(current, now)
            worker_dead = current.workerPid is not None and not _worker_process_group_alive(
                current.workerPid
            )
            if not stale and not (fresh_dead_worker and worker_dead):
                return
        reconciled = reconcile_closeout_mutations(current)
        recovery_commits = derive_closeout_recovery_commits(current, mutations=reconciled)
        if reconciled == current.mutationEvidence and recovery_commits == current.recoveryCommits:
            return
        projected = current.model_copy(
            update={
                "mutationEvidence": reconciled,
                "recoveryCommits": recovery_commits,
                "irreversibleBoundaryEntered": (
                    current.irreversibleBoundaryEntered
                    or any(item.state == "commit-proven" for item in reconciled.values())
                ),
            }
        )
        updated, matched = store.update_if_current(
            current,
            lambda _record, projected=projected: projected,
        )
        if matched:
            return
        current = updated


def launch_detached_worker(contract: WorktreeContract, record: LifecycleOperationRecord) -> None:
    if record.operationKind == "direct-landing":
        raise RuntimeError("direct landing is synchronous and cannot launch a detached worker")
    require_linux_worker_runtime()
    reports_root = contract.worktree_group / "reports"
    report = Path(record.reportPath)
    atomic_write_text(report, "")
    env = native_subprocess_environment(git_environment(), temp_root=reports_root / "tmp")
    worker_lease = fingerprint_payload(
        {
            "operationKey": record.operationKey,
            "generation": record.generation,
            "attempt": record.attempt,
            "queuedAt": record.queuedAt,
        }
    )
    command = native_command(
        [
            sys.executable,
            "-m",
            "agents_remember.application.lifecycle.lifecycle_operation_worker",
            "--contract-path",
            record.contractPath,
            "--kind",
            record.operationKind,
            "--worker-lease",
            worker_lease,
        ],
        env,
    )
    with report.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=contract.code_worktree,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    retain_detached_worker_child(process)
    fingerprint = worker_process_fingerprint(process.pid)
    if fingerprint is None:
        fallback_fingerprint = fingerprint_payload(
            {
                "state": "unverified-spawned-worker",
                "pid": process.pid,
                "workerLease": worker_lease,
                "operationKey": record.operationKey,
                "generation": record.generation,
                "attempt": record.attempt,
            }
        )
        _store(contract, record.operationKind).update(
            lambda current: current.model_copy(
                update={
                    "status": "termination-required",
                    "phase": "termination-required",
                    "workerPid": process.pid,
                    "workerLease": worker_lease,
                    "workerProcessFingerprint": fallback_fingerprint,
                    "workerTermination": WorkerTerminationEvidence(
                        state="termination-required",
                        pid=process.pid,
                        lease=worker_lease,
                        processFingerprint=fallback_fingerprint,
                        requestedAt=now_iso(),
                        detail=(
                            "spawned worker identity could not be captured; no further "
                            "worker may launch until this exact process group exits"
                        ),
                    ),
                    "terminationReturnStatus": current.status,
                    "terminationReturnPhase": current.phase,
                    "currentCommand": "prove unverified spawned worker group exited",
                }
            )
        )
        raise RuntimeError(
            "detached worker process identity could not be captured; "
            "termination-required authority was retained"
        )
    _store(contract, record.operationKind).update(
        lambda current: (
            current.model_copy(
                update={
                    "workerPid": process.pid,
                    "workerLease": worker_lease,
                    "workerProcessFingerprint": fingerprint,
                }
            )
            if current.status == "queued" and current.fingerprint == record.fingerprint
            else current
        )
    )


def queued_operation_record(
    contract: WorktreeContract,
    operation_input: LifecycleOperationInput,
    candidate: LifecycleOperationCandidate,
    integration_authority: IntegrationOperationAuthority | None,
    timestamp: datetime,
) -> LifecycleOperationRecord:
    stamp = timestamp.isoformat()
    record = LifecycleOperationRecord(
        taskId=contract.task_id,
        taskName=contract.task_name,
        contractPath=contract.contract_path.as_posix(),
        operationKind=operation_input.kind,
        candidateState=candidate.state,
        candidateTree=candidate.tree,
        taskIntent=candidate.task_intent,
        fingerprint=candidate.fingerprint,
        operationKey=operation_key(
            contract.contract_path, operation_input.kind, candidate.fingerprint
        ),
        integrationAuthority=integration_authority,
        input=operation_input,
        status="queued",
        phase="queued",
        queuedAt=stamp,
        currentCommand=f"waiting to start {operation_input.kind}",
        reportPath=located_lifecycle_operation_report_path(
            contract,
            operation_input.kind,
        ).as_posix(),
        mutationEvidence=(
            initial_closeout_mutation_evidence(contract, operation_input.effectiveInput)
            if isinstance(operation_input, CloseoutOperationInput)
            else {}
        ),
    )
    if operation_input.kind == "integrate":
        return record.model_copy(update={"dependencies": lifecycle_operation_dependencies(record)})
    return record


def _integration_authority(
    contract: WorktreeContract, operation_input: IntegrateOperationInput
) -> IntegrationOperationAuthority:
    if contract.closeout_status != "completed" or not contract.code_commit:
        raise RuntimeError("integration authority requires a completed closeout code commit")
    targets = {target.side: target for target in integration_targets(contract)}
    code_target = targets["code"]
    code_source_commit = branch_commit(contract.code_repo_path, code_target.branch)
    code_replay_required = not is_ancestor(
        contract.code_repo_path, code_source_commit, contract.code_commit
    )
    memory_source_commit = ""
    memory_replay_required = False
    if contract.memory_mode == "external":
        if (
            contract.memory_repo_path is None
            or not contract.memory_content_commit
            or not contract.ledger_commit
        ):
            raise RuntimeError(
                "external-memory integration authority requires repo and closeout commits"
            )
        memory_target = targets["memory"]
        memory_source_commit = branch_commit(contract.memory_repo_path, memory_target.branch)
        memory_replay_required = not is_ancestor(
            contract.memory_repo_path, memory_source_commit, contract.ledger_commit
        )
    else:
        memory_target = None
    conflict = None
    if operation_input.strategy == "replay" and (code_replay_required or memory_replay_required):
        if contract.kind == "series":
            raise RuntimeError(
                "atomic series integration cannot open a leaf conflict worktree; source drift "
                "requires orchestrator-owned block recovery or graph reshape"
            )
        conflict = IntegrationConflictTransaction(
            codeReplayRequired=code_replay_required,
            memoryReplayRequired=memory_replay_required,
            codeSourceRef=f"refs/heads/{code_target.branch}",
            codeSourceCommit=code_source_commit,
            codeCandidateCommit=contract.code_commit,
            memorySourceRef=(
                f"refs/heads/{memory_target.branch}" if memory_target is not None else ""
            ),
            memorySourceCommit=memory_source_commit,
            memoryContentCommit=contract.memory_content_commit,
            ledgerCommit=contract.ledger_commit,
            codeWorktree=contract.code_worktree.resolve().as_posix(),
            memoryWorktree=(
                contract.memory_worktree.resolve().as_posix()
                if contract.memory_worktree is not None
                else ""
            ),
        )
    return IntegrationOperationAuthority(
        targetKind=code_target.kind,
        codeRepository=code_target.repository.as_posix(),
        codeSourceBranch=code_target.branch,
        codeSourceRef=f"refs/heads/{code_target.branch}",
        codeSourceCommit=code_source_commit,
        codeCandidateCommit=contract.code_commit,
        memoryRepository=(memory_target.repository.as_posix() if memory_target is not None else ""),
        memorySourceBranch=(memory_target.branch if memory_target is not None else ""),
        memorySourceRef=(f"refs/heads/{memory_target.branch}" if memory_target is not None else ""),
        memorySourceCommit=memory_source_commit,
        memoryContentCommit=contract.memory_content_commit,
        ledgerCommit=contract.ledger_commit,
        conflictTransaction=conflict,
    )


def _recoverable_stale(record: LifecycleOperationRecord, now: datetime) -> bool:
    if record.status not in {"queued", "running"}:
        return False
    stamp = parse_operation_stamp(record.heartbeatAt or record.queuedAt)
    stale = (now - stamp).total_seconds() > STALE_HEARTBEAT_SECONDS
    if not stale or record.workerPid is None:
        return stale
    return not _worker_process_group_alive(record.workerPid)


def _worker_process_group_alive(pid: int) -> bool:
    """Treat a reused/live process group as owned until a human resolves the stale record."""

    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _validate_input_identity(
    contract: WorktreeContract, operation_input: LifecycleOperationInput
) -> None:
    if Path(operation_input.contractPath).resolve() != contract.contract_path.resolve():
        raise RuntimeError("lifecycle operation input does not resolve to its loaded contract")
    if isinstance(operation_input, CloseoutOperationInput):
        if not operation_input.approvalNote.strip():
            raise RuntimeError("closeout apply requires a non-empty approval intent note")
        require_effective_closeout_plan(contract, operation_input.effectiveInput, route="worktree")


def _store(contract: WorktreeContract, kind: LifecycleOperationKind) -> LifecycleOperationStore:
    return located_lifecycle_operation_store(contract, kind)
