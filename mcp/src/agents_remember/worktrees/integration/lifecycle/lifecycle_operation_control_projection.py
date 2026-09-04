"""Public legal-control projection from durable lifecycle evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.direct_landing import DirectLandingOperationInput
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
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
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    located_lifecycle_operation_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    classify_migrated_lifecycle,
)
from agents_remember.worktrees.integration.lifecycle.worker.termination import (
    worker_exit_unproven,
)
from agents_remember.worktrees.modules.git import branch_commit, is_ancestor, require_git
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class LifecycleControlProjectionContext:
    """Caller authority plus the two live observations read for one projection."""

    allow_completed_disposition: bool = False
    caller: DeclaredCaller | None = None
    integration: IntegrationOperationObservation | None = None
    door: DoorPublicationClassification | None = None


def legal_operation_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    *,
    context: LifecycleControlProjectionContext | None = None,
) -> list[dict[str, Any]]:
    """Derive only controls whose public handler can revalidate this generation."""

    context = context or LifecycleControlProjectionContext()
    base = _control_arguments(contract, record, context.caller)
    publication = record.doorPublication
    if publication is not None and publication.state == "intent":
        observed_door = context.door or classify_door_publication(publication, contract)
        if observed_door.state == "developer-decision":
            return []
        pending = _pending_door_control(
            contract,
            record,
            base,
            allow_completed_disposition=context.allow_completed_disposition,
        )
        return _without_legacy_generation_reuse(
            record,
            [pending] if pending is not None else [],
        )
    integration_observation, evidence_controls = _evidence_controls(
        contract,
        record,
        base,
        context.integration,
    )
    if evidence_controls is not None:
        return _without_legacy_generation_reuse(record, evidence_controls)
    migration = classify_migrated_lifecycle(record)
    if migration.state == "terminal" and record.status == "failed":
        return []
    if record.status == "termination-required":
        controls = [_control("cancel", base, "Complete exact same-generation cancellation.")]
    elif worker_exit_unproven(record):
        controls = [_control("cancel", base, "Retry exact worker termination and prove exit.")]
    elif record.status == "cancelled":
        controls = _cancelled_controls(
            contract,
            record,
            base,
            integration_observation=integration_observation,
        )
    elif record.status == "completed":
        controls = _completed_controls(
            contract,
            record,
            base,
            allow_completed_disposition=context.allow_completed_disposition,
        )
    else:
        controls = _nonterminal_controls(
            record,
            base,
            integration_observation=integration_observation,
        )
    return _without_legacy_generation_reuse(record, controls)


def _without_legacy_generation_reuse(
    record: LifecycleOperationRecord,
    controls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if record.operationKind not in {"closeout", "direct-landing"} or isinstance(
        record.taskIntent, TaskIntentIdentity
    ):
        return controls
    return [control for control in controls if control["action"] not in {"recover", "retry"}]


def _evidence_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    observation: IntegrationOperationObservation | None,
) -> tuple[IntegrationOperationObservation | None, list[dict[str, Any]] | None]:
    recovery_controls = _recovery_evidence_controls(contract, record)
    if recovery_controls is not None:
        return observation, recovery_controls
    observation = _integration_observation(
        contract,
        record,
        observation,
    )
    if observation is not None and observation.decision is not None:
        return observation, []
    repair_controls = _repair_controls(observation, base)
    if repair_controls is not None:
        return observation, repair_controls
    door_blocked = bool(
        observation is not None and observation.door is not None and not observation.door.valid
    )
    if door_blocked:
        return observation, []
    return observation, None


def _recovery_evidence_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> list[dict[str, Any]] | None:
    initial_door = classify_initial_closeout_door_recovery(contract, record)
    if initial_door.state == "developer-decision":
        return []
    ledger_recovery = classify_closeout_ledger_recovery(contract, record)
    direct_recovery = classify_direct_landing_recovery(contract, record)
    if (
        ledger_recovery.state == "developer-decision"
        or direct_recovery.state == "developer-decision"
    ):
        return []
    return None


def _integration_observation(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    observation: IntegrationOperationObservation | None,
) -> IntegrationOperationObservation | None:
    if record.operationKind != "integrate":
        return None
    return observation or classify_integration_operation(contract, record)


def _repair_controls(
    observation: IntegrationOperationObservation | None,
    base: dict[str, object],
) -> list[dict[str, Any]] | None:
    if observation is None or observation.repair.state == "not-applicable":
        return None
    if observation.repair.state == "accepted":
        return [_control("cancel", base, "Publish and prove the exact contract reset.")]
    return []


def _control_arguments(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    caller: DeclaredCaller | None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "contract_path": contract.contract_path.as_posix(),
        "operation_kind": record.operationKind,
        "expected_generation": record.generation,
        "intent_note": "<developer intent>",
        "dry_run": False,
    }
    if caller is not None:
        base["caller"] = caller.model_dump(mode="json")
    return base


def _nonterminal_controls(
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    integration_observation: IntegrationOperationObservation | None,
) -> list[dict[str, Any]]:
    if record.operationKind == "integrate":
        assert integration_observation is not None
        if (
            integration_observation.refs is not None
            and integration_observation.refs.state == "conflict"
        ):
            return []
    if generation_requires_recovery(record):
        return [_control("recover", base, "Reconcile and continue this exact generation.")]
    if record.operationKind == "direct-landing":
        return [
            _control("recover", base, "Execute the admitted synchronous generation."),
            _control("cancel", base, "Cancel after proving both Git legs unchanged."),
        ]
    controls = [_control("cancel", base, "Cancel after proving no output and worker exit.")]
    if record.operationKind == "closeout":
        controls.append(_revise_control(record, base))
    if record.status == "queued" and record.workerPid is None:
        controls.insert(
            0,
            _control("recover", base, "Launch the accepted generation after publication."),
        )
    if record.status in {"failed", "input-required"}:
        controls.insert(
            0,
            _control("retry", base, "Retry the same immutable input and candidate."),
        )
    return controls


def generation_requires_recovery(record: LifecycleOperationRecord) -> bool:
    """Whether durable output authority requires same-generation recovery."""

    if record.operationKind in {"closeout", "direct-landing"}:
        return closeout_generation_retained(record)
    return record.irreversibleBoundaryEntered or record.integrationPublication is not None


def _pending_door_control(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    allow_completed_disposition: bool,
) -> dict[str, Any] | None:
    publication = record.doorPublication
    if publication is None or publication.state != "intent":
        return None
    disposition = publication.generation.disposition
    action = pending_door_action(record)
    if action is None:
        return None
    if action in {"retire", "supersede"} and not allow_completed_disposition:
        return None
    if action == "revise":
        return _revise_control(record, base)
    if action == "supersede":
        return _supersede_control(contract, record, base)
    return _control(
        action,
        base,
        f"Finish the exact pending {disposition} closeout-door publication.",
    )


def pending_door_action(
    record: LifecycleOperationRecord,
) -> str | None:
    """Return the one same-handler action that resumes a journaled door intent."""

    publication = record.doorPublication
    if publication is None or publication.state != "intent":
        return None
    if publication.generation.disposition == "claimed":
        return "recover"
    if publication.generation.disposition == "waiting":
        if record.generationDisposition == "superseded":
            return "supersede"
        if record.status == "cancelled":
            return "cancel"
    return None


def _cancelled_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    integration_observation: IntegrationOperationObservation | None,
) -> list[dict[str, Any]]:
    publication = record.doorPublication
    if publication is not None and publication.state == "intent":
        return [_control("cancel", base, "Finish the durable cancellation disposition.")]
    if record.operationKind == "closeout":
        return [_revise_control(record, base)]
    if record.operationKind == "direct-landing":
        return [_direct_successor_control(contract, record)]
    assert integration_observation is not None
    return _cancelled_integration_controls(
        contract,
        record,
        base,
        integration_observation=integration_observation,
    )


def _cancelled_integration_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    integration_observation: IntegrationOperationObservation,
) -> list[dict[str, Any]]:
    if record.organizationalRepair is not None:
        if integration_observation.repair.state == "accepted":
            return [_control("cancel", base, "Prove the exact journal-owned contract reset.")]
        if integration_observation.repair.state != "not-applicable":
            return []
    if contract.closeout_status != "completed":
        return []
    if operation_state_fingerprint(contract) != record.candidateState:
        return [_integration_control(contract, summary="Start a freshly validated successor.")]
    return []


def _completed_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    allow_completed_disposition: bool,
) -> list[dict[str, Any]]:
    if record.operationKind == "direct-landing":
        return _completed_direct_controls(
            contract,
            record,
            base,
            allow_completed_disposition=allow_completed_disposition,
        )
    if record.operationKind != "closeout":
        return []
    return _completed_closeout_controls(
        contract,
        record,
        base,
        allow_completed_disposition=allow_completed_disposition,
    )


def _completed_direct_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    allow_completed_disposition: bool,
) -> list[dict[str, Any]]:
    if _is_exact_direct_successor(contract, record):
        return [_direct_successor_control(contract, record)]
    if (_is_exact_direct_owner(contract, record), allow_completed_disposition) != (True, True):
        return []
    return _completed_direct_owner_controls(contract, record, base)


def _is_exact_direct_successor(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> bool:
    publication = record.doorPublication
    observed = (
        record.generationDisposition,
        publication.state if publication is not None else None,
        publication.generation.disposition if publication is not None else None,
        contract.closeout_door if publication is None else publication.generation,
    )
    return observed == ("superseded", "proven", "waiting", contract.closeout_door)


def _is_exact_direct_owner(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> bool:
    publication = record.doorPublication
    if publication is None:
        return False
    observed = (
        record.generationDisposition,
        publication.state,
        publication.generation.disposition,
        publication.generation.operationFingerprint,
        publication.generation.claimedOperationKey,
        publication.generation,
    )
    expected = (
        "active",
        "proven",
        "claimed",
        record.fingerprint,
        record.operationKey,
        contract.closeout_door,
    )
    return observed == expected


def _completed_direct_owner_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
) -> list[dict[str, Any]]:
    controls = [_control("retire", base, "Retire this completed direct generation for audit.")]
    if _direct_code_candidate_advanced(contract, record):
        controls.append(_supersede_control(contract, record, base))
    return controls


def _completed_closeout_controls(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
    *,
    allow_completed_disposition: bool,
) -> list[dict[str, Any]]:
    if record.generationDisposition == "superseded":
        publication = record.doorPublication
        exact_successor = bool(
            publication is not None
            and publication.generation.disposition == "waiting"
            and (publication.state == "intent" or contract.closeout_door == publication.generation)
        )
        return (
            [_supersede_control(contract, record, base)]
            if allow_completed_disposition and exact_successor
            else []
        )
    exact_owner = (
        contract.integration_status != "completed"
        and record.generationDisposition == "active"
        and closeout_generation_retained(record)
        and record.closeoutFinalizedContractSha256 == closeout_contract_sha256(contract)
    )
    if not exact_owner or _integration_claim_active(contract):
        return []
    controls = [_integration_control(contract, summary="Integrate the exact completed generation.")]
    if allow_completed_disposition:
        controls.extend(
            (
                _control("retire", base, "Retire the completed unintegrated generation."),
                _supersede_control(contract, record, base),
            )
        )
    return controls


def _integration_claim_active(contract: WorktreeContract) -> bool:
    try:
        record = located_lifecycle_operation_store(contract, "integrate").read()
    except LifecycleOperationReadError:
        return True
    return bool(
        record is not None
        and record.integrationPublication is not None
        and record.status not in {"cancelled", "failed"}
    )


def _supersede_control(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    base: dict[str, object],
) -> dict[str, Any]:
    publication = record.doorPublication
    door = (
        publication.generation
        if publication is not None and publication.generation.disposition == "waiting"
        else contract.closeout_door
    )
    if door is None:
        return _control("supersede", base, "Publish one fresh waiting door successor.")
    control = _control("supersede", base, "Publish one fresh waiting door successor.")
    control["arguments"].update(
        {
            "grade": {
                "priority": door.schedulingProvenance.priority,
                "judgmentId": door.schedulingProvenance.judgmentId,
            },
            "admission": {
                "resourceReady": door.admissionProvenance.resourceReady,
                "resourceReason": door.admissionProvenance.resourceReason,
                "admissionReady": door.admissionProvenance.admissionReady,
                "admissionReason": door.admissionProvenance.admissionReason,
            },
        }
    )
    return control


def _direct_code_candidate_advanced(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> bool:
    operation_input = record.input
    if not isinstance(operation_input, DirectLandingOperationInput):
        return False
    current = branch_commit(contract.code_repo_path, contract.code_work_branch)
    return current != operation_input.codeCommit and is_ancestor(
        contract.code_repo_path,
        operation_input.codeCommit,
        current,
    )


def _control(action: str, base: dict[str, object], summary: str) -> dict[str, Any]:
    generation_effect = {
        "retry": "same",
        "recover": "same",
        "cancel": "terminal-disposition",
        "revise": "successor",
        "retire": "terminal-disposition",
        "supersede": "terminal-disposition",
    }[action]
    return {
        "action": action,
        "generationEffect": generation_effect,
        "tool": "worktree_operation_control",
        "arguments": {**base, "action": action},
        "summary": summary,
    }


def _revise_control(
    record: LifecycleOperationRecord,
    base: dict[str, object],
) -> dict[str, Any]:
    control = _control("revise", base, "Validate and publish one fresh successor.")
    operation_input = record.input
    effective = getattr(operation_input, "effectiveInput", None)
    arguments = control["arguments"]
    for leg in ("code", "memory", "ledger"):
        arguments[f"{leg}_commit_message"] = (
            f"<fresh nonblank {leg} commit message>"
            if effective is not None and effective.enabled(leg)
            else None
        )
    return control


def _integration_control(
    contract: WorktreeContract,
    *,
    summary: str,
) -> dict[str, Any]:
    return {
        "action": "integrate",
        "generationEffect": "successor",
        "tool": "worktree_integrate",
        "arguments": {
            "contract_path": contract.contract_path.as_posix(),
            "strategy": "ff-only",
            "ledger_commit_message": "",
            "dry_run": False,
        },
        "summary": summary,
    }


def _direct_successor_control(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> dict[str, Any]:
    code_commit = branch_commit(contract.code_repo_path, contract.code_work_branch)
    candidate_tree = require_git(
        contract.code_repo_path,
        ["rev-parse", f"{code_commit}^{{tree}}"],
    )
    return {
        "action": "direct-landing",
        "generationEffect": "successor",
        "tool": "direct_landing",
        "arguments": {
            "contract_path": record.contractPath,
            "code_commit": code_commit,
            "candidate_tree": candidate_tree,
            "memory_commit_message": "<nonblank memory commit message>",
            "ledger_commit_message": "<nonblank ledger commit message>",
            "intent_note": "<developer intent>",
            "dry_run": False,
        },
        "summary": "Start one fresh direct-landing successor after proven disposition.",
    }
