"""Post-publication projection-scope union for authoritative task changes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument
from agents_remember.tasks.document_field_effects import (
    TaskDocumentMutationClassification,
    classify_task_document_mutation,
)
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology


class TaskDocScopeError(ValueError):
    """A prepared changed-document set cannot identify its canonical task addresses."""


@dataclass(frozen=True)
class TaskDocScopeChange:
    """One accepted before/candidate pair used only to derive projection blast radius."""

    ref: TaskDocumentRef
    original: TaskDocument | None
    candidate: TaskDocument
    classification: TaskDocumentMutationClassification = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "classification",
            classify_task_document_mutation(self.original, self.candidate),
        )


def resolve_projection_scope_union(
    coordination_root: Path,
    repo_id: str,
    changes: tuple[TaskDocScopeChange, ...],
) -> tuple[TaskDocumentRef, ...]:
    """Return the complete canonical old/new sprint union for one published task batch."""

    topology = TaskDocumentTopology(coordination_root)
    by_ref: dict[TaskDocumentRef, TaskDocScopeChange] = {}
    for change in changes:
        if change.ref.repository != repo_id:
            raise TaskDocScopeError(
                f"scope change {change.ref.key} does not belong to repository {repo_id!r}"
            )
        previous = by_ref.get(change.ref)
        if previous is not None and previous != change:
            raise TaskDocScopeError(f"conflicting scope changes for {change.ref.key}")
        by_ref[change.ref] = change

    overrides = {ref: change.candidate for ref, change in by_ref.items()}
    scopes: set[TaskDocumentRef] = set()
    for change in by_ref.values():
        if not change.classification.invalidates_projection:
            continue
        versions = tuple(
            document for document in (change.original, change.candidate) if document is not None
        )
        if any(_is_sprint(document) for document in versions):
            scopes.add(change.ref)
        if any(document.kind == "master" and not document.orchestrates for document in versions):
            scopes.update(
                sprint.ref
                for sprint in topology.projection_sprints_affected_by_master(
                    change.ref,
                    original=change.original,
                    candidate=change.candidate,
                    overrides=overrides,
                )
            )
        if any(document.kind == "subTask" for document in versions):
            scopes.update(
                _leaf_projection_scopes(
                    topology,
                    change,
                    by_ref,
                    overrides,
                )
            )
    return tuple(sorted(scopes, key=lambda ref: ref.key))


def _is_sprint(document: TaskDocument) -> bool:
    return document.kind == "master" and bool(document.orchestrates)


def _leaf_projection_scopes(
    topology: TaskDocumentTopology,
    leaf: TaskDocScopeChange,
    changes: dict[TaskDocumentRef, TaskDocScopeChange],
    overrides: dict[TaskDocumentRef, TaskDocument],
) -> set[TaskDocumentRef]:
    parent_ref = TaskDocumentRef(
        repository=leaf.ref.repository,
        path=(Path(leaf.ref.path).parent / "task.json").as_posix(),
    )
    parent_change = changes.get(parent_ref)
    if parent_change is None:
        try:
            current = topology.resolve(parent_ref, overrides).document
        except TaskDocumentRefError:
            return set()
        original = current
        candidate = current
    else:
        original = parent_change.original
        candidate = parent_change.candidate
    return {
        sprint.ref
        for sprint in topology.projection_sprints_affected_by_master(
            parent_ref,
            original=original,
            candidate=candidate,
            overrides=overrides,
        )
    }
