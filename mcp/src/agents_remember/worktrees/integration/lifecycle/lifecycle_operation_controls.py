"""Task-addressed lifecycle controls derived from durable and live evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agents_remember.controlplane.integration_authority_lock import integration_authority_lock
from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.closeout.input import (
    CloseoutCorrectedCall,
    CloseoutMessageInput,
)
from agents_remember.models.closeout.source import CandidateAdmissionFacts, SchedulingGradeInput
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    GatePolicyRuleSnapshot,
    LifecycleOperationKind,
    LifecycleOperationProjection,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleControlAction
from agents_remember.models.lifecycles.operation_projection import LifecycleRecommendedAction
from agents_remember.worktrees.closeout_input import (
    CloseoutInputError,
    corrected_closeout_arguments,
)
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationClassification,
    classify_door_publication,
    prepare_door_publication,
)
from agents_remember.worktrees.integration.closeout.door_source import (
    superseding_door_generation,
)
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    classify_closeout_ledger_recovery,
)
from agents_remember.worktrees.integration.closeout.operation_admission import (
    CloseoutOperationAdmission,
    ValidatedCloseoutAdmission,
    prevalidate_closeout_operation_admission,
)
from agents_remember.worktrees.integration.configured_contract_authority import (
    reread_configured_contract,
)
from agents_remember.worktrees.integration.integration_operation_decision import (
    IntegrationOperationObservation,
    classify_integration_operation,
    raise_integration_decision,
    require_integration_operation_convergent,
)
from agents_remember.worktrees.integration.integration_ref_state import classify_integration_refs
from agents_remember.worktrees.integration.lifecycle.control.cancellation import cancel_operation
from agents_remember.worktrees.integration.lifecycle.lifecycle_completed_disposition import (
    require_completed_disposition,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_generation_resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    fingerprint_payload,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    LifecycleControlProjectionContext,
    generation_requires_recovery,
    legal_operation_controls,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_door_control import (
    complete_pending_door,
    complete_pending_door_locked,
    project_closeout_refresh,
    record_door_intent,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    contract_lifecycle_lease,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_live_decision import (
    raise_live_evidence_decision,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    bind_projection_result,
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_recovery import (
    reconcile_control_mutations,
    recover_direct_landing_under_authority,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    launch_detached_worker,
)
from agents_remember.worktrees.integration.lifecycle.worker.state import (
    project_worker_exit,
    reconcile_worker_exit,
)
from agents_remember.worktrees.queue.closeout_queue import CloseoutQueueError
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract


@dataclass(frozen=True)
class LifecycleControlCommand:
    admitted_contract: WorktreeContract
    admitted_location: LifecycleOperationLocation
    configured_authority: str
    kind: LifecycleOperationKind
    action: LifecycleControlAction
    expected_generation: int
    intent_note: str
    dry_run: bool = False
    revision_messages: CloseoutMessageInput | None = None
    revision_gate_policy: list[GatePolicyRuleSnapshot] | None = None
    supersede_grade: SchedulingGradeInput | None = None
    supersede_admission: CandidateAdmissionFacts | None = None
    allow_completed_disposition: bool = False
    caller: DeclaredCaller | None = None


@dataclass(frozen=True)
class _ControlObservation:
    contract: WorktreeContract
    store: LifecycleOperationStore
    record: LifecycleOperationRecord
    integration: IntegrationOperationObservation | None
    door: DoorPublicationClassification | None


@dataclass(frozen=True)
class _SupersedeSource:
    grade: SchedulingGradeInput
    admission: CandidateAdmissionFacts
    caller: DeclaredCaller
    declaration_fingerprint: str


def _reload_control_contract(
    command: LifecycleControlCommand,
) -> tuple[WorktreeContract, LifecycleOperationLocation]:
    """Revalidate current configured contract and locator truth under the lease."""
    return reread_configured_contract(
        command.admitted_contract,
        command.configured_authority,
    )


def control_operation(
    command: LifecycleControlCommand,
) -> LifecycleOperationProjection:
    """Execute one same-generation or terminal-disposition control."""
    if not command.intent_note.strip():
        raise LifecycleControlError(
            "lifecycle-control-intent-required",
            "lifecycle control requires a nonblank developer intent note",
            next_action=command.action,
        )
    with contract_lifecycle_lease(
        command.admitted_contract,
        location=command.admitted_location,
    ):
        contract, location = _reload_control_contract(command)
        require_lifecycle_operation_compatible(
            contract,
            operation_kind=command.kind,
            publish_worker_exits=not command.dry_run,
        )
        store = LifecycleOperationStore(location.journal_path(command.kind))
        observed = _observe_control_under_lease(command, contract=contract, store=store)
        legal_rows = legal_operation_controls(
            observed.contract,
            observed.record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=command.allow_completed_disposition,
                caller=command.caller,
                integration=observed.integration,
                door=observed.door,
            ),
        )
        legal = {item["action"] for item in legal_rows}
        if command.action not in legal:
            raise_live_evidence_decision(
                observed.contract,
                observed.record,
                integration_observation=observed.integration,
            )
            if (
                command.action == "cancel"
                and observed.record.status == "cancelled"
                and observed.record.operationKind == "integrate"
                and observed.record.organizationalRepair is not None
            ):
                # A previously advertised repair payload must still classify an
                # exact post-crash reset or third byte state even when status no
                # longer advertises cancellation for the contradiction.
                return cancel_operation(
                    observed.contract,
                    observed.store,
                    observed.record,
                    dry_run=command.dry_run,
                )
            next_row = legal_rows[0] if legal_rows else None
            next_action = next_row["action"] if next_row else "developer-decision"
            raise LifecycleControlError(
                "lifecycle-control-not-legal",
                "the requested action is not legal for the current generation evidence",
                expected={"legalActions": sorted(legal)},
                observed={"requestedAction": command.action, "status": observed.record.status},
                next_action=next_action,
                next_tool=next_row["tool"] if next_row else None,
                next_args=next_row["arguments"] if next_row else None,
            )
        return _execute_legal_control(
            observed.contract,
            observed.store,
            observed.record,
            command,
        )


def _observe_control_under_lease(
    command: LifecycleControlCommand,
    *,
    contract: WorktreeContract,
    store: LifecycleOperationStore,
) -> _ControlObservation:
    """Return one newest record and every legal-action classifier derived from it."""

    record = _require_generation(store, command, contract=contract)
    record = (
        project_worker_exit(record) if command.dry_run else reconcile_worker_exit(store) or record
    )
    record = reconcile_control_mutations(
        store,
        record,
        dry_run=command.dry_run,
        preserve_recovery_intent=command.action == "recover",
    )
    integration = None
    if command.kind == "integrate":
        with integration_authority_lock(
            contract.coordination_root,
            contract.repo_name,
            create=not command.dry_run,
        ):
            contract, location = _reload_control_contract(command)
            store = LifecycleOperationStore(location.journal_path(command.kind))
            record = _require_generation(store, command, contract=contract)
            record = (
                project_worker_exit(record)
                if command.dry_run
                else reconcile_worker_exit(store) or record
            )
            integration = classify_integration_operation(contract, record)
            require_integration_operation_convergent(integration)
    publication = record.doorPublication
    door = (
        classify_door_publication(publication, contract)
        if publication is not None and publication.state == "intent"
        else None
    )
    if door is not None and door.state == "developer-decision":
        payload = door.decision_payload()
        raise LifecycleControlError(
            str(payload["state"]),
            str(payload["decisionSurface"]),
            expected=door.expected,
            observed=door.observed,
            next_action="developer-decision",
        )
    return _ControlObservation(contract, store, record, integration, door)


def _execute_legal_control(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    command: LifecycleControlCommand,
) -> LifecycleOperationProjection:
    action = command.action
    if action == "cancel":
        return cancel_operation(contract, store, record, dry_run=command.dry_run)
    if action == "retry":
        return _resume(
            contract,
            store,
            record,
            action="retry",
            dry_run=command.dry_run,
        )
    if action == "recover":
        return _resume(
            contract,
            store,
            record,
            action="recover",
            dry_run=command.dry_run,
        )
    if action == "retire":
        return _dispose_completed(
            contract,
            store,
            record,
            command=command,
        )
    if action == "supersede":
        return _dispose_completed(
            contract,
            store,
            record,
            command=command,
        )
    return _revise_closeout(
        contract,
        store,
        record,
        command=command,
    )


def _resume(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    action: Literal["retry", "recover"],
    dry_run: bool,
) -> LifecycleOperationProjection:
    if action == "recover" and record.operationKind == "closeout":
        ledger_recovery = classify_closeout_ledger_recovery(contract, record)
        if ledger_recovery.state == "developer-decision":
            raise LifecycleControlError(
                ledger_recovery.status,
                ledger_recovery.detail,
                expected=ledger_recovery.expected,
                observed=ledger_recovery.observed,
                next_action="developer-decision",
            )
    _require_resumable(record, action)
    if dry_run:
        return operation_projection(record, contract=contract)
    contract, record = _resume_closeout_publications(contract, store, record)
    if record.operationKind == "direct-landing":
        with integration_authority_lock(contract.coordination_root, contract.repo_name):
            current_contract, _location = reread_configured_contract(
                contract,
                record.input.configPath,
            )
            _require_proven_closeout_door_for_launch(current_contract, record)
            current = recover_direct_landing_under_authority(current_contract, store, record)
            return operation_projection(
                current,
                contract=load_contract(current_contract.contract_path),
            )
    requeued, changed = store.resume_generation(
        requeued_same_generation,
        expected_generation=record.generation,
    )
    if not changed:
        raise LifecycleControlError(
            "lifecycle-generation-changed",
            "a newer lifecycle generation replaced the advertised action",
            expected={"generation": record.generation},
            observed={"generation": requeued.generation},
            next_action="recover",
        )
    launch_detached_worker(contract, requeued)
    current = store.read() or requeued
    return operation_projection(current, contract=load_contract(contract.contract_path))


def _resume_closeout_publications(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
) -> tuple[WorktreeContract, LifecycleOperationRecord]:
    if record.operationKind not in {"closeout", "direct-landing"}:
        return contract, record
    if record.legacyMigration is not None and record.doorPublication is None:
        # The explicit schema-1 bridge proves a retained generation that predates
        # contract-owned closeout doors.  Normal schema-3 records never bypass the
        # claimed-door launch gate.
        return contract, record
    if record.doorPublication is None:
        raise LifecycleControlError(
            "closeout-initial-door-intent-missing",
            "the canonical closeout record is missing its create-time claimed-door intent",
            expected={"doorPublication": "create-time-claimed-intent-or-proof"},
            observed={"doorPublication": "absent", "generation": record.generation},
            next_action="developer-decision",
        )
    elif record.doorPublication.state == "intent":
        record = complete_pending_door(contract, store, record, dry_run=False)
        contract = load_contract(contract.contract_path)
    _require_proven_closeout_door_for_launch(contract, record)
    return contract, record


def _require_proven_closeout_door_for_launch(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> None:
    publication = record.doorPublication
    observed_door = contract.closeout_door
    if (
        publication is not None
        and publication.state == "proven"
        and publication.generation.disposition == "claimed"
        and publication.generation.operationKind == record.operationKind
        and publication.generation.operationFingerprint == record.fingerprint
        and publication.generation.claimedOperationKey == record.operationKey
        and observed_door == publication.generation
    ):
        return
    raise LifecycleControlError(
        "closeout-claimed-door-proof-required",
        "closeout recovery cannot launch before its exact claimed door is proven",
        expected={
            "generation": record.generation,
            "operationFingerprint": record.fingerprint,
            "publicationState": "proven",
            "doorDisposition": "claimed",
            "doorGenerationId": (
                publication.generation.generationId if publication is not None else ""
            ),
        },
        observed={
            "publication": (
                publication.model_dump(mode="json") if publication is not None else None
            ),
            "contractDoor": (
                observed_door.model_dump(mode="json") if observed_door is not None else None
            ),
        },
        next_action="developer-decision",
    )


def _require_resumable(
    record: LifecycleOperationRecord,
    action: Literal["retry", "recover"],
) -> None:
    if record.status in {"completed", "cancelled"}:
        raise LifecycleControlError(
            "lifecycle-generation-terminal",
            "same-generation retry/recover cannot replace this terminal disposition",
            next_action="revise" if record.status == "cancelled" else "retire",
        )
    if record.workerPid is not None:
        raise LifecycleControlError(
            "lifecycle-worker-still-authoritative",
            "the exact worker binding is still live; retry would create duplicate authority",
            observed={"pid": record.workerPid, "lease": record.workerLease or ""},
            next_action="cancel",
        )
    if record.operationKind == "integrate":
        facts = classify_integration_refs(record)
        if facts.state == "conflict":
            raise_integration_decision(facts.decision_payload())
        if action == "retry" and facts.state == "intended":
            raise LifecycleControlError(
                "integration-ref-recovery-required",
                "protected refs moved within this generation; retry cannot replace it",
                expected={"before": facts.before, "intended": facts.intended},
                observed=facts.observed_payload(),
                next_action="recover",
            )
    if action == "retry" and generation_requires_recovery(record):
        raise LifecycleControlError(
            "lifecycle-recover-required",
            "output intent/proof belongs to recover, not retry",
            next_action="recover",
        )


def _dispose_completed(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    command: LifecycleControlCommand,
) -> LifecycleOperationProjection:
    if command.action == "retire":
        return _retire_completed(contract, store, record, dry_run=command.dry_run)
    assert command.action == "supersede"
    source = _supersede_source(command, record)
    if record.generationDisposition == "superseded":
        return _resume_completed_supersede(
            contract,
            store,
            record,
            declaration_fingerprint=source.declaration_fingerprint,
            dry_run=command.dry_run,
        )
    require_completed_disposition(contract, record, "supersede")
    runtime = load_config(command.configured_authority)
    if command.dry_run:
        return _preview_completed_supersede(
            runtime,
            contract,
            record,
            source,
        )
    return _publish_completed_supersede(
        runtime,
        contract,
        store,
        record,
        source,
    )


def _retire_completed(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    dry_run: bool,
) -> LifecycleOperationProjection:
    if record.generationDisposition == "retired":
        return operation_projection(record, contract=contract)
    require_completed_disposition(contract, record, "retire")
    if dry_run:
        return operation_projection(record, contract=contract)
    updated = store.update(
        lambda current: current.model_copy(
            update={
                "generationDisposition": "retired",
                "guidance": "This completed generation is retired for audit only.",
            }
        )
    )
    return operation_projection(updated, contract=contract)


def _supersede_source(
    command: LifecycleControlCommand,
    record: LifecycleOperationRecord,
) -> _SupersedeSource:
    grade = command.supersede_grade
    admission = command.supersede_admission
    caller = command.caller
    if grade is None or admission is None or caller is None:
        raise LifecycleControlError(
            "lifecycle-supersede-source-required",
            "supersede requires fresh scheduling, admission, and authorized caller evidence",
            next_action="supersede",
        )
    return _SupersedeSource(
        grade,
        admission,
        caller,
        _supersede_declaration_fingerprint(
            record,
            grade=grade,
            admission=admission,
            caller=caller,
        ),
    )


def _resume_completed_supersede(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    declaration_fingerprint: str,
    dry_run: bool,
) -> LifecycleOperationProjection:
    _require_supersede_declaration_match(record, declaration_fingerprint)
    current_contract = contract
    if not dry_run:
        operation_input = record.input
        with task_publication_lock(contract.coordination_root, contract.repo_name):
            current_contract, _location = reread_configured_contract(
                contract,
                operation_input.configPath,
            )
            current_record = store.read()
            if current_record is None or current_record.generationDisposition != "superseded":
                raise LifecycleControlError(
                    "lifecycle-generation-changed",
                    "the superseded generation changed before replay",
                    next_action="developer-decision",
                )
            _require_supersede_declaration_match(current_record, declaration_fingerprint)
            record = complete_pending_door_locked(current_contract, store, current_record)
            current_contract = load_contract(current_contract.contract_path)
    _require_waiting_supersede_proof(current_contract, record)
    projection = operation_projection(record, contract=current_contract)
    return project_closeout_refresh(
        projection,
        current_contract,
        record,
        dry_run=dry_run,
    )


def _require_waiting_supersede_proof(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> None:
    publication = record.doorPublication
    if (
        publication is None
        or publication.generation.disposition != "waiting"
        or (publication.state == "proven" and contract.closeout_door != publication.generation)
    ):
        raise LifecycleControlError(
            "closeout-door-supersede-proof-required",
            "superseded journal history does not retain its exact waiting door successor",
            next_action="developer-decision",
        )


def _preview_completed_supersede(
    runtime,
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    source: _SupersedeSource,
) -> LifecycleOperationProjection:
    try:
        successor = superseding_door_generation(
            runtime,
            contract,
            actor=source.caller,
            grade=source.grade,
            admission=source.admission,
        )
    except CloseoutQueueError as exc:
        raise LifecycleControlError(
            exc.status,
            "fresh supersede door evidence is not admissible",
            next_action="supersede",
        ) from exc
    return bind_projection_result(
        operation_projection(record, contract=contract),
        {
            "state": "would-supersede",
            "doorGeneration": successor.model_dump(mode="json"),
        },
    )


def _publish_completed_supersede(
    runtime,
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    source: _SupersedeSource,
) -> LifecycleOperationProjection:

    operation_input = record.input
    with task_publication_lock(contract.coordination_root, contract.repo_name):
        current_contract, _location = reread_configured_contract(
            contract,
            operation_input.configPath,
        )
        current_record = store.read()
        if current_record is not None and current_record.generationDisposition == "superseded":
            _require_supersede_declaration_match(
                current_record,
                source.declaration_fingerprint,
            )
            updated = complete_pending_door_locked(
                current_contract,
                store,
                current_record,
            )
            current_contract = load_contract(current_contract.contract_path)
        elif current_record is None or current_record != record:
            raise LifecycleControlError(
                "lifecycle-generation-changed",
                "the completed generation changed before supersede publication",
                expected={"generation": record.generation, "fingerprint": record.fingerprint},
                observed={
                    "generation": current_record.generation if current_record is not None else 0,
                    "fingerprint": current_record.fingerprint if current_record is not None else "",
                },
                next_action="developer-decision",
            )
        else:
            require_completed_disposition(current_contract, current_record, "supersede")
            try:
                successor = superseding_door_generation(
                    runtime,
                    current_contract,
                    actor=source.caller,
                    grade=source.grade,
                    admission=source.admission,
                )
            except CloseoutQueueError as exc:
                raise LifecycleControlError(
                    exc.status,
                    "fresh supersede door evidence is not admissible",
                    next_action="supersede",
                ) from exc
            intent = prepare_door_publication(current_contract, successor)
            updated = store.update(
                lambda current: record_door_intent(
                    current.model_copy(
                        update={
                            "generationDisposition": "superseded",
                            "supersedeDeclarationFingerprint": source.declaration_fingerprint,
                            "guidance": (
                                "A distinct current-source waiting door successor is published."
                            ),
                        }
                    ),
                    intent,
                    generation_disposition="superseded",
                )
            )
            updated = complete_pending_door_locked(current_contract, store, updated)
            current_contract = load_contract(current_contract.contract_path)
    projection = operation_projection(updated, contract=current_contract)
    return project_closeout_refresh(
        projection,
        current_contract,
        updated,
        dry_run=False,
    )


def _supersede_declaration_fingerprint(
    record: LifecycleOperationRecord,
    *,
    grade: SchedulingGradeInput,
    admission: CandidateAdmissionFacts,
    caller: DeclaredCaller,
) -> str:
    return fingerprint_payload(
        {
            "schema": "closeout-supersede-declaration/v1",
            "operationFingerprint": record.fingerprint,
            "grade": grade.model_dump(mode="json"),
            "admission": admission.model_dump(mode="json"),
            "caller": caller.model_dump(mode="json"),
        }
    )


def _require_supersede_declaration_match(
    record: LifecycleOperationRecord,
    requested: str,
) -> None:
    accepted = record.supersedeDeclarationFingerprint
    if accepted is None:
        raise LifecycleControlError(
            "lifecycle-supersede-declaration-proof-missing",
            "the canonical superseded journal is missing its accepted declaration fingerprint",
            next_action="developer-decision",
        )
    if accepted != requested:
        raise LifecycleControlError(
            "lifecycle-supersede-declaration-conflict",
            "a competing supersede declaration cannot replay the accepted successor",
            expected={"supersedeDeclarationFingerprint": accepted},
            observed={"supersedeDeclarationFingerprint": requested},
            next_action="developer-decision",
        )


def _revise_closeout(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    command: LifecycleControlCommand,
) -> LifecycleOperationProjection:
    if record.operationKind != "closeout" or command.revision_messages is None:
        raise LifecycleControlError(
            "lifecycle-revision-input-required",
            "closeout revise requires fresh explicit commit-message fields",
            next_action="revise",
        )
    if record.status == "cancelled":
        record = complete_pending_door(contract, store, record, dry_run=command.dry_run)
        cancellation_projection = operation_projection(record, contract=contract)
    else:
        cancellation_projection = cancel_operation(
            contract,
            store,
            record,
            dry_run=command.dry_run,
        )
        record = store.read() or record
    current_contract = load_contract(contract.contract_path)
    validated = _validated_revision(current_contract, record, command)
    state = "would-revise" if command.dry_run else "revision-ready"
    result = {
        "state": state,
        "summary": (
            "Cancellation would publish a distinct waiting door successor; apply the "
            "validated closeout input through worktree_closeout_apply."
            if command.dry_run
            else "A distinct waiting door successor is published; apply the validated "
            "closeout input through worktree_closeout_apply."
        ),
        "nextAction": "apply-closeout-successor",
        "nextTool": "worktree_closeout_apply",
        "nextArgs": _revision_apply_args(validated),
    }
    summary = str(result["summary"])
    return bind_projection_result(
        cancellation_projection,
        result,
        guidance=summary,
        recommendation=LifecycleRecommendedAction(
            action="apply-closeout-successor",
            tool="worktree_closeout_apply",
            arguments=_revision_apply_args(validated),
            summary=summary,
            mutating=True,
        ),
    )


def _validated_revision(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    command: LifecycleControlCommand,
) -> ValidatedCloseoutAdmission:
    admission = _closeout_revision_admission(record, command)
    try:
        validated = prevalidate_closeout_operation_admission(contract, admission)
    except CloseoutInputError as exc:
        raise LifecycleControlError(
            exc.status,
            "closeout revision input is invalid; use the corrected fields",
            expected=exc.response_fields(),
            next_action="revise",
        ) from exc
    if (
        validated.operation_input == record.input
        or validated.candidate.fingerprint == record.fingerprint
    ):
        raise LifecycleControlError(
            "lifecycle-revision-unchanged",
            "revise requires genuinely fresh approved intent",
            next_action="revise",
        )
    return validated


def _revision_apply_args(validated: ValidatedCloseoutAdmission) -> dict[str, object]:
    operation_input = validated.operation_input
    args: dict[str, object] = {
        "contract_path": operation_input.contractPath,
        "intent_note": operation_input.approvalNote,
        "dry_run": False,
    }
    for leg, field in (
        ("code", "code_commit_message"),
        ("memory", "memory_commit_message"),
        ("ledger", "ledger_commit_message"),
    ):
        accepted = getattr(operation_input.effectiveInput, leg)
        if accepted.state == "enabled":
            args[field] = accepted.message
    return args


def _closeout_revision_admission(
    record: LifecycleOperationRecord,
    command: LifecycleControlCommand,
) -> CloseoutOperationAdmission:
    operation_input = record.input
    config_path = operation_input.configPath
    gate_policy = command.revision_gate_policy
    if gate_policy is None:
        raise LifecycleControlError(
            "lifecycle-revision-policy-required",
            "closeout revise requires the current configured gate policy snapshot",
            next_action="revise",
        )
    return CloseoutOperationAdmission(
        config_path=config_path,
        contract_path=Path(record.contractPath),
        messages=command.revision_messages or CloseoutMessageInput(),
        approval_note=command.intent_note.strip(),
        gate_policy=gate_policy,
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_operation_control",
            arguments={
                **corrected_closeout_arguments(
                    record.contractPath,
                    operation_kind="closeout",
                    action="revise",
                    expected_generation=record.generation,
                    intent_note="<fresh developer intent>",
                )
            },
        ),
    )


def _require_generation(
    store: LifecycleOperationStore,
    command: LifecycleControlCommand,
    *,
    contract: WorktreeContract,
) -> LifecycleOperationRecord:
    record = store.read()
    if record is None:
        raise LifecycleControlError(
            "lifecycle-operation-missing",
            "no lifecycle operation exists for the task and kind",
            next_action="developer-decision",
        )
    if record.generation != command.expected_generation:
        legal_rows = legal_operation_controls(
            contract,
            record,
            context=LifecycleControlProjectionContext(
                allow_completed_disposition=command.allow_completed_disposition,
                caller=command.caller,
            ),
        )
        next_row = legal_rows[0] if legal_rows else None
        raise LifecycleControlError(
            "lifecycle-generation-changed",
            "the advertised lifecycle generation is stale",
            expected={"generation": command.expected_generation},
            observed={"generation": record.generation},
            next_action=next_row["action"] if next_row else "developer-decision",
            next_tool=next_row["tool"] if next_row else None,
            next_args=next_row["arguments"] if next_row else None,
        )
    return record
