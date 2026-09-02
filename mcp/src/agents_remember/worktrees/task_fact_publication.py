"""One task-first publication boundary shared by every task-truth writer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents_remember.controlplane.closeout_queue_store import CloseoutQueueStore
from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.models.closeout.projection import (
    ProjectionInvalidationResult,
    ProjectionRebuildResult,
    ProjectionSourceProblem,
    TaskDocProjectionEffect,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks.document import TaskDocument
from agents_remember.tasks.document_field_effects import classify_task_document_mutation
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology
from agents_remember.tasks.store import json_path_for
from agents_remember.worktrees.queue.closeout_projection import (
    now_iso,
)
from agents_remember.worktrees.queue.closeout_projection_publication import (
    ProjectionInvalidationReceipt,
    preview_closeout_projection_effect,
    rebuild_action,
    rebuild_invalidated_closeout_projection,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class TaskFactPublicationResult[T]:
    result: T
    projection_effects: tuple[TaskDocProjectionEffect, ...]


def publish_task_fact_mutation[T](
    coordination_root: Path,
    repo_id: str,
    *,
    validate: Callable[[], None],
    projection_scopes: Callable[[], tuple[TaskDocumentRef, ...]],
    publication: Callable[[], T],
) -> TaskFactPublicationResult[T]:
    """Publish truth and invalidate its complete fixed-order scope union under one CAS."""

    timestamp = now_iso()
    with task_publication_lock(coordination_root, repo_id):
        validate()
        scopes = tuple(sorted(set(projection_scopes()), key=lambda ref: ref.key))
        result = publication()
        receipts = tuple(
            _invalidate_scope(coordination_root, sprint_ref, timestamp) for sprint_ref in scopes
        )
    effects: list[TaskDocProjectionEffect] = []
    for receipt in receipts:
        if receipt.invalidation.outcome == "failed":
            effects.append(_invalidation_failure_effect(receipt))
            continue
        try:
            effects.append(
                rebuild_invalidated_closeout_projection(
                    coordination_root,
                    receipt,
                )
            )
        except Exception as exc:
            effects.append(_rebuild_failure_effect(receipt, exc))
    return TaskFactPublicationResult(result, tuple(effects))


def validate_task_fact_mutation(
    coordination_root: Path,
    repo_id: str,
    validate: Callable[[], None],
) -> None:
    with task_publication_lock(coordination_root, repo_id, create=False):
        validate()


def contract_projection_scopes(
    contract: WorktreeContract,
    documents: tuple[TaskDocument, ...] = (),
) -> tuple[TaskDocumentRef, ...]:
    """Resolve every current sprint whose projection consumes this contract's task root."""

    topology = TaskDocumentTopology(contract.coordination_root)
    overrides = _contract_task_overrides(contract, documents, topology)
    if not any(
        classify_task_document_mutation(
            topology.resolve(ref).document,
            candidate,
        ).invalidates_projection
        for ref, candidate in overrides.items()
    ):
        return ()
    try:
        master_ref = topology.canonical_ref(contract.repo_name, contract.task_root / "task.json")
        master = topology.resolve(master_ref)
    except TaskDocumentRefError as exc:
        if exc.status == "task-document-not-found":
            return ()
        raise
    affected = topology.projection_sprints_affected_by_master(
        master_ref,
        original=master.document,
        candidate=overrides.get(master_ref, master.document),
        overrides=overrides,
    )
    return tuple(sorted({sprint.ref for sprint in affected}, key=lambda ref: ref.key))


def publish_contract_task_facts[T](
    contract: WorktreeContract,
    publication: Callable[[], T],
    *,
    documents: tuple[TaskDocument, ...] = (),
    validate: Callable[[], None] = lambda: None,
) -> TaskFactPublicationResult[T]:
    return publish_task_fact_mutation(
        contract.coordination_root,
        contract.repo_name,
        validate=validate,
        projection_scopes=lambda: contract_projection_scopes(contract, documents),
        publication=publication,
    )


def preview_contract_task_facts(
    contract: WorktreeContract,
    documents: tuple[TaskDocument, ...],
) -> tuple[TaskDocProjectionEffect, ...]:
    """Project a lifecycle-owned task batch without publishing task or projection bytes."""

    topology = TaskDocumentTopology(contract.coordination_root)
    overrides = _contract_task_overrides(contract, documents, topology)
    return tuple(
        preview_closeout_projection_effect(
            contract.coordination_root,
            sprint_ref,
            overrides=overrides,
        )
        for sprint_ref in contract_projection_scopes(contract, documents)
    )


def _contract_task_overrides(
    contract: WorktreeContract,
    documents: tuple[TaskDocument, ...],
    topology: TaskDocumentTopology,
) -> dict[TaskDocumentRef, TaskDocument]:
    return {
        topology.canonical_ref(
            contract.repo_name,
            json_path_for(contract.task_root, document),
        ): document
        for document in documents
    }


def _invalidate_scope(
    coordination_root: Path,
    sprint_ref: TaskDocumentRef,
    timestamp: str,
) -> ProjectionInvalidationReceipt:
    store: CloseoutQueueStore | None = None
    existed = False
    prior_revision: int | None = None
    prior_fingerprint: str | None = None
    try:
        store = CloseoutQueueStore(coordination_root, sprint_ref)
        # A presence-inspection failure is not canonical absence. Retain the conservative
        # present signal if ``exists`` cannot complete, then return its typed per-scope effect.
        existed = True
        existed = store.exists()
        prior = store.read_raw(timestamp=timestamp)
        prior_revision = prior.revision if existed else None
        prior_fingerprint = prior.sourceFingerprint
        _invalid, invalidation = store.invalidate(timestamp=timestamp)
    except Exception as exc:
        invalidation = ProjectionInvalidationResult(
            outcome="failed",
            diagnostic=_projection_problem(store, sprint_ref, exc),
        )
    return ProjectionInvalidationReceipt(
        sprint_ref,
        existed,
        prior_revision,
        prior_fingerprint,
        invalidation,
    )


def _projection_problem(
    store: CloseoutQueueStore | None,
    sprint_ref: TaskDocumentRef,
    error: Exception,
) -> ProjectionSourceProblem:
    return ProjectionSourceProblem(
        kind="projection",
        address=store.state_path.as_posix() if store is not None else sprint_ref.key,
        state="unreadable",
        errorType=type(error).__name__,
        repairAction=rebuild_action(sprint_ref),
    )


def _invalidation_failure_effect(
    receipt: ProjectionInvalidationReceipt,
) -> TaskDocProjectionEffect:
    diagnostic = receipt.invalidation.diagnostic
    assert diagnostic is not None
    return TaskDocProjectionEffect(
        sprintTaskDocumentRef=receipt.sprint_ref,
        queueExisted=receipt.queue_existed,
        priorRevision=receipt.prior_revision,
        priorSourceFingerprint=receipt.prior_source_fingerprint,
        invalidation=receipt.invalidation,
        rebuild=ProjectionRebuildResult(outcome="not-attempted", sourceProblems=[diagnostic]),
        nextAction=rebuild_action(receipt.sprint_ref),
    )


def _rebuild_failure_effect(
    receipt: ProjectionInvalidationReceipt,
    error: Exception,
) -> TaskDocProjectionEffect:
    diagnostic = ProjectionSourceProblem(
        kind="projection",
        address=receipt.sprint_ref.key,
        state="unreadable",
        errorType=type(error).__name__,
        repairAction=rebuild_action(receipt.sprint_ref),
    )
    return TaskDocProjectionEffect(
        sprintTaskDocumentRef=receipt.sprint_ref,
        queueExisted=receipt.queue_existed,
        priorRevision=receipt.prior_revision,
        priorSourceFingerprint=receipt.prior_source_fingerprint,
        invalidation=receipt.invalidation,
        rebuild=ProjectionRebuildResult(
            outcome="source-unreadable",
            sourceProblems=[diagnostic],
        ),
        nextAction=rebuild_action(receipt.sprint_ref),
    )
