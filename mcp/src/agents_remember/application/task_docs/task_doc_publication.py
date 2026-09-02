"""Task-first publication and disposable projection refresh."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.closeout.projection import TaskDocProjectionEffect
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    TaskDocSourceReadError,
    TaskDocSourceSnapshot,
    TaskDocument,
    current_task_doc_source,
    json_path_for,
    read_graph_titles,
    write_task_docs,
)
from agents_remember.tasks.document_refs import TaskDocumentTopology
from agents_remember.worktrees.queue.closeout_projection_publication import (
    preview_closeout_projection_effect,
)
from agents_remember.worktrees.task_fact_publication import (
    publish_task_fact_mutation,
    validate_task_fact_mutation,
)

from .task_doc_graph_titles import require_single_graph_document
from .task_doc_queue_scope import TaskDocScopeChange, resolve_projection_scope_union
from .task_doc_route_review import TaskDocError


class TaskDocPublicationConflict(TaskDocError):
    """A selected JSON/Markdown source changed after the candidate was prepared."""

    def __init__(
        self,
        *,
        expected: dict[str, object],
        observed: dict[str, object],
    ) -> None:
        self.status = "task-document-publication-conflict"
        self.expected = expected
        self.observed = observed
        super().__init__(
            "task-document-publication-conflict: selected task JSON or Markdown changed "
            "after this edit was prepared; re-read and retry the task-addressed edit"
        )


@dataclass(frozen=True)
class TaskDocPublication:
    config: McpRuntimeConfig
    target_repo_id: str
    task_root: Path
    original: TaskDocument | None
    candidate: TaskDocument
    documents: list[TaskDocument]
    source_snapshots: tuple[TaskDocSourceSnapshot, ...]
    publisher: Callable[[], list[tuple[Path, Path]]] | None = None


@dataclass(frozen=True)
class TaskDocPublicationTransaction:
    """One exact source-pair CAS and authoritative task publication."""

    coordination_root: Path
    target_repo_id: str
    source_snapshots: tuple[TaskDocSourceSnapshot, ...]
    scope_changes: tuple[TaskDocScopeChange, ...]
    publisher: Callable[[], list[tuple[Path, Path]]]


@dataclass(frozen=True)
class TaskDocPublicationResult:
    written: list[tuple[Path, Path]]
    projection_effects: tuple[TaskDocProjectionEffect, ...]


def publish_task_doc_set(context: TaskDocPublication) -> TaskDocPublicationResult:
    """Publish task truth first, then independently refresh every affected scope."""

    transaction = task_doc_publication_transaction(context)
    return publish_task_doc_transaction_and_refresh(transaction)


def publish_prepared_task_documents(
    coordination_root: Path,
    target_repo_id: str,
    task_root: Path,
    documents: list[TaskDocument],
    source_snapshots: tuple[TaskDocSourceSnapshot, ...],
) -> TaskDocPublicationResult:
    """Publish an already-prepared document set through the one task-truth transaction.

    Callers own candidate construction, but never reconstruct source CAS, projection scope,
    invalidation, rebuild, or the underlying JSON/Markdown writer vocabulary.
    """

    graph_document = require_single_graph_document(documents)
    graph = graph_document.executionGraph if graph_document is not None else None
    overrides = _prepared_task_doc_overrides(
        coordination_root,
        target_repo_id,
        task_root,
        documents,
    )
    transaction = TaskDocPublicationTransaction(
        coordination_root=coordination_root,
        target_repo_id=target_repo_id,
        source_snapshots=source_snapshots,
        scope_changes=task_doc_scope_changes(
            coordination_root,
            target_repo_id,
            overrides,
            source_snapshots,
        ),
        publisher=lambda: write_task_docs(
            task_root,
            documents,
            graph_titles=(
                read_graph_titles(task_root.parents[1], graph) if graph is not None else None
            ),
        ),
    )
    return publish_task_doc_transaction_and_refresh(transaction)


def publish_task_doc_transaction_and_refresh(
    transaction: TaskDocPublicationTransaction,
) -> TaskDocPublicationResult:
    """Commit task truth and invalidate every scope under CAS, then rebuild independently."""

    published = publish_task_fact_mutation(
        transaction.coordination_root,
        transaction.target_repo_id,
        validate=lambda: require_task_doc_sources_current(transaction.source_snapshots),
        projection_scopes=lambda: resolve_projection_scope_union(
            transaction.coordination_root,
            transaction.target_repo_id,
            transaction.scope_changes,
        ),
        publication=transaction.publisher,
    )
    return TaskDocPublicationResult(published.result, published.projection_effects)


def preview_task_doc_projection_effects(
    context: TaskDocPublication,
) -> tuple[TaskDocProjectionEffect, ...]:
    """Predict the post-publication projections against exact candidate task bytes."""

    return preview_task_doc_transaction_projection_effects(
        task_doc_publication_transaction(context)
    )


def preview_task_doc_transaction_projection_effects(
    transaction: TaskDocPublicationTransaction,
) -> tuple[TaskDocProjectionEffect, ...]:
    overrides = {change.ref: change.candidate for change in transaction.scope_changes}
    return tuple(
        preview_closeout_projection_effect(
            transaction.coordination_root,
            sprint_ref,
            overrides=overrides,
        )
        for sprint_ref in resolve_projection_scope_union(
            transaction.coordination_root,
            transaction.target_repo_id,
            transaction.scope_changes,
        )
    )


def task_doc_publication_transaction(
    context: TaskDocPublication,
) -> TaskDocPublicationTransaction:
    """Build the sole exact transaction for one ordinary/remove task-doc candidate."""

    graph_document = require_single_graph_document(context.documents)
    graph = graph_document.executionGraph if graph_document is not None else None
    overrides = _task_doc_publication_overrides(context)
    return TaskDocPublicationTransaction(
        coordination_root=context.config.coordination_root,
        target_repo_id=context.target_repo_id,
        source_snapshots=context.source_snapshots,
        scope_changes=task_doc_scope_changes(
            context.config.coordination_root,
            context.target_repo_id,
            overrides,
            context.source_snapshots,
        ),
        publisher=context.publisher
        or (
            lambda: write_task_docs(
                context.task_root,
                context.documents,
                graph_titles=(
                    read_graph_titles(context.task_root.parents[1], graph)
                    if graph is not None
                    else None
                ),
            )
        ),
    )


def task_doc_scope_changes(
    coordination_root: Path,
    repo_id: str,
    overrides: dict[TaskDocumentRef, TaskDocument],
    source_snapshots: tuple[TaskDocSourceSnapshot, ...],
) -> tuple[TaskDocScopeChange, ...]:
    """Bind every changed candidate to its exact accepted original source bytes."""

    topology = TaskDocumentTopology(coordination_root)
    sources = {snapshot.json_path.resolve(strict=False): snapshot for snapshot in source_snapshots}
    changes: list[TaskDocScopeChange] = []
    for ref, candidate in sorted(overrides.items(), key=lambda item: item[0].key):
        path = topology.path_for_ref(ref).resolve(strict=False)
        accepted = sources.get(path)
        if accepted is None:
            raise TaskDocError(f"changed task document has no accepted source snapshot: {ref.key}")
        try:
            original = (
                None
                if accepted.json_bytes is None
                else TaskDocument.model_validate_json(accepted.json_bytes)
            )
        except ValueError as exc:
            raise TaskDocError(
                f"accepted task source cannot derive projection scope: {ref.key}: {exc}"
            ) from exc
        if candidate.repo != repo_id:
            raise TaskDocError(
                f"task document {ref.key} declares repo {candidate.repo!r}, expected {repo_id!r}"
            )
        if original is not None and original.repo != repo_id:
            raise TaskDocError(
                f"accepted task document {ref.key} declares repo {original.repo!r}, "
                f"expected {repo_id!r}"
            )
        changes.append(TaskDocScopeChange(ref, original, candidate))
    return tuple(changes)


def validate_task_doc_transaction(transaction: TaskDocPublicationTransaction) -> None:
    """Read-only dry-run preflight through the same exact source-pair transaction."""

    def validate() -> None:
        require_task_doc_sources_current(transaction.source_snapshots)
        resolve_projection_scope_union(
            transaction.coordination_root,
            transaction.target_repo_id,
            transaction.scope_changes,
        )

    validate_task_fact_mutation(
        transaction.coordination_root,
        transaction.target_repo_id,
        validate,
    )


def require_task_doc_sources_current(
    snapshots: tuple[TaskDocSourceSnapshot, ...],
) -> None:
    """Exact-CAS precondition shared by preview and protected publication."""

    for accepted in snapshots:
        try:
            current = current_task_doc_source(accepted)
        except TaskDocSourceReadError as exc:
            raise TaskDocPublicationConflict(
                expected=accepted.evidence(),
                observed={"readFailure": exc.evidence()},
            ) from exc
        if current != accepted:
            raise TaskDocPublicationConflict(
                expected=accepted.evidence(),
                observed=current.evidence(),
            )


def _task_doc_publication_overrides(
    context: TaskDocPublication,
) -> dict[TaskDocumentRef, TaskDocument]:
    return _prepared_task_doc_overrides(
        context.config.coordination_root,
        context.target_repo_id,
        context.task_root,
        context.documents,
    )


def _prepared_task_doc_overrides(
    coordination_root: Path,
    target_repo_id: str,
    task_root: Path,
    documents: list[TaskDocument],
) -> dict[TaskDocumentRef, TaskDocument]:
    root = (coordination_root / "tasks" / target_repo_id).resolve(strict=False)
    overrides: dict[TaskDocumentRef, TaskDocument] = {}
    for document in documents:
        path = json_path_for(task_root, document).resolve(strict=False)
        if not path.is_relative_to(root):
            raise TaskDocError(f"task document publication escapes tasks root: {path}")
        ref = TaskDocumentRef(
            repository=target_repo_id,
            path=path.relative_to(root).as_posix(),
        )
        overrides[ref] = document
    return overrides
