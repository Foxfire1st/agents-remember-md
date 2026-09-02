"""Journal-owned closeout-door publication and disposable projection refresh."""

from __future__ import annotations

from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationProjection,
    LifecycleOperationRecord,
    lifecycle_operation_dependencies,
)
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationError,
    publish_door_intent,
)
from agents_remember.worktrees.integration.configured_contract_authority import (
    reread_configured_contract,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_projection import (
    LifecycleControlProjectionContext,
    legal_operation_controls,
    pending_door_action,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.queue.closeout_projection_publication import (
    projection_refresh_failure_effect,
    refresh_closeout_projection,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


def record_door_intent(
    record: LifecycleOperationRecord,
    intent,
    *,
    generation_disposition: str,
) -> LifecycleOperationRecord:
    history = list(record.doorPublicationHistory)
    if record.doorPublication is not None:
        if record.doorPublication.state != "proven":
            raise RuntimeError("unfinished door publication must complete before another begins")
        history.append(record.doorPublication)
    updated = record.model_copy(
        update={
            "doorPublication": intent,
            "doorPublicationHistory": history,
            "generationDisposition": generation_disposition,
        }
    )
    return updated.model_copy(update={"dependencies": lifecycle_operation_dependencies(updated)})


def complete_pending_door(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    *,
    dry_run: bool,
) -> LifecycleOperationRecord:
    intent = record.doorPublication
    if intent is None or intent.state == "proven" or dry_run:
        return record
    operation_input = record.input
    if record.operationKind not in {"closeout", "direct-landing"}:
        raise RuntimeError("door publication intent belongs only to schedulable commit operations")
    with task_publication_lock(contract.coordination_root, contract.repo_name):
        current_contract, _location = reread_configured_contract(
            contract,
            operation_input.configPath,
        )
        return complete_pending_door_locked(current_contract, store, record)


def complete_pending_door_locked(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
) -> LifecycleOperationRecord:
    intent = record.doorPublication
    if intent is None or intent.state == "proven":
        return record
    try:
        proof = publish_door_intent(contract.contract_path, intent)
    except DoorPublicationError as exc:
        classification = exc.classification
        if classification.state == "accepted-before":
            action = pending_door_action(record) or "recover"
            next_row = next(
                (
                    item
                    for item in legal_operation_controls(
                        contract,
                        record,
                        context=LifecycleControlProjectionContext(
                            allow_completed_disposition=True,
                            door=classification,
                        ),
                    )
                    if item["action"] == action
                ),
                None,
            )
            raise LifecycleControlError(
                "closeout-door-publication-interrupted",
                "the journaled closeout-door publication did not change contract bytes",
                expected=classification.expected,
                observed=classification.observed,
                next_action=action,
                next_tool=next_row["tool"] if next_row else None,
                next_args=next_row["arguments"] if next_row else None,
            ) from exc
        raise LifecycleControlError(
            "closeout-door-publication-conflict",
            exc.detail,
            expected=classification.expected,
            observed=classification.observed,
            next_action="developer-decision",
        ) from exc
    return store.update(lambda current: current.model_copy(update={"doorPublication": proof}))


def project_closeout_refresh(
    projection: LifecycleOperationProjection,
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    *,
    dry_run: bool,
) -> LifecycleOperationProjection:
    if dry_run or record.operationKind not in {"closeout", "direct-landing"}:
        return projection
    door = contract.closeout_door
    if door is None:
        return projection
    try:
        effect = refresh_closeout_projection(
            contract.coordination_root,
            door.sprintTaskDocumentRef,
        )
    except Exception as exc:
        effect = projection_refresh_failure_effect(
            contract.coordination_root,
            door.sprintTaskDocumentRef,
            exc,
        )
    return projection.model_copy(update={"projectionEffects": [effect]})


__all__ = [
    "complete_pending_door",
    "complete_pending_door_locked",
    "project_closeout_refresh",
    "record_door_intent",
]
