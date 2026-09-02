"""One task-intent source for closeout, door, and lifecycle consumers."""

from __future__ import annotations

from agents_remember.errors import TaskIntentError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
)
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.tasks.task_intent import (
    require_current_task_intent,
    task_intent_identity,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


def contract_task_intent_candidate(
    contract: WorktreeContract,
    *,
    candidate_ref: TaskDocumentRef | None = None,
) -> ResolvedTaskDocument:
    """Resolve the exact contract-owned leaf without accepting caller prose."""

    topology = TaskDocumentTopology(contract.coordination_root)
    try:
        if candidate_ref is not None:
            candidate = topology.resolve(candidate_ref)
        elif contract.kind == "leaf":
            found = resolve_terminal_leaf_doc(contract.task_root, contract.leaf_id)
            if found is None:
                raise TaskIntentError(
                    "task-intent-task-document-missing",
                    f"leaf {contract.leaf_id!r} has no canonical task document",
                )
            candidate = topology.resolve(topology.canonical_ref(contract.repo_name, found[0]))
        else:
            raise TaskIntentError(
                "task-intent-candidate-required",
                "series closeout requires its exact typed candidate task-document reference",
            )
    except TaskDocumentRefError as exc:
        raise TaskIntentError(exc.status, str(exc)) from exc
    if not candidate.path.resolve().is_relative_to(contract.task_root.resolve()):
        raise TaskIntentError(
            "task-intent-task-document-outside-root",
            "closeout candidate task document is outside the contract task root",
        )
    if candidate.document.kind == "master":
        raise TaskIntentError(
            "task-intent-leaf-required",
            "closeout task intent must resolve to one leaf task document",
        )
    return candidate


def contract_task_intent(
    contract: WorktreeContract,
    *,
    candidate_ref: TaskDocumentRef | None = None,
) -> TaskIntentIdentity:
    candidate = contract_task_intent_candidate(contract, candidate_ref=candidate_ref)
    return task_intent_identity(contract.task_root, candidate)


def current_door_task_intent(contract: WorktreeContract) -> TaskIntentIdentity:
    """Require the live door to bind the exact current canonical leaf intent."""

    door = contract.closeout_door
    if door is None:
        raise TaskIntentError(
            "closeout-door-missing",
            "closeout admission requires a current closeout-door generation",
            next_action="closeout_door.declare",
        )
    current = contract_task_intent(contract, candidate_ref=door.taskDocumentRef)
    return require_current_task_intent(
        door.taskIntent,
        current,
        owner="closeout-door",
        next_action="closeout_door.update-provenance",
    )


__all__ = [
    "contract_task_intent",
    "contract_task_intent_candidate",
    "current_door_task_intent",
]
