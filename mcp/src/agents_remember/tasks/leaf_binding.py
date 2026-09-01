"""Canonical composite binding between a master row and one JSON-primary leaf."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from agents_remember.errors import AgentsRememberError
from agents_remember.models.task_document_ref import TaskDocumentRef

from .document import SubTaskRef, TaskDocument


class CanonicalLeafBindingError(AgentsRememberError):
    """One parent row, child address, and child document do not agree exactly."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class CanonicalLeafSource:
    """The direct Markdown rendering and JSON-primary ref declared by one row."""

    markdown_path: PurePosixPath
    json_ref: TaskDocumentRef


@dataclass(frozen=True)
class CanonicalLeafBinding:
    """One exact parent row and its exact canonical child source."""

    row: SubTaskRef
    source: CanonicalLeafSource


@dataclass(frozen=True)
class _LeafRowMatches:
    numbered: list[tuple[int, SubTaskRef]]
    sourced: list[tuple[int, SubTaskRef]]
    stemmed: list[tuple[int, SubTaskRef]]


def require_leaf_parent_row(parent: TaskDocument, leaf_id: str) -> SubTaskRef:
    """Select exactly one parent row by child document ID."""

    if parent.kind != "master":
        raise CanonicalLeafBindingError(
            "task-leaf-binding-parent-invalid",
            "canonical leaf binding requires a master parent document",
        )
    rows = [row for row in parent.subTasks if row.number == leaf_id]
    if len(rows) != 1:
        status = "task-leaf-binding-row-missing" if not rows else "task-leaf-binding-row-ambiguous"
        raise CanonicalLeafBindingError(
            status,
            "parent task must contain exactly one live row "
            f"for leaf {leaf_id!r}; found {len(rows)}",
        )
    return rows[0]


def canonical_leaf_source(
    parent_ref: TaskDocumentRef,
    row: SubTaskRef,
) -> CanonicalLeafSource:
    """Derive one confined direct child source from its parent row."""

    if row.masterRef is not None:
        raise CanonicalLeafBindingError(
            "task-leaf-binding-row-invalid",
            "a leaf parent row cannot carry a sprint masterRef",
        )
    parent_path = PurePosixPath(parent_ref.path)
    if parent_path.name != "task.json":
        raise CanonicalLeafBindingError(
            "task-leaf-binding-parent-invalid",
            f"leaf parent must use a canonical task.json address: {parent_ref.key}",
        )
    file_value = row.file.strip()
    relative = PurePosixPath(file_value)
    if (
        not file_value
        or relative.is_absolute()
        or relative.parent != PurePosixPath(".")
        or relative.suffix != ".md"
    ):
        raise CanonicalLeafBindingError(
            "task-leaf-binding-row-invalid",
            "leaf parent row must name one direct Markdown child source",
        )
    markdown = parent_path.parent / relative
    return CanonicalLeafSource(
        markdown_path=markdown,
        json_ref=TaskDocumentRef(
            repository=parent_ref.repository,
            path=markdown.with_suffix(".json").as_posix(),
        ),
    )


def require_canonical_leaf_binding(
    parent_ref: TaskDocumentRef,
    parent: TaskDocument,
    candidate_ref: TaskDocumentRef,
    candidate: TaskDocument,
) -> CanonicalLeafBinding:
    """Require candidate ID, direct child source, and one parent row to agree."""

    _require_binding_documents(parent, candidate)
    _require_direct_child_candidate(parent_ref, candidate_ref)
    matches = _candidate_row_matches(
        parent_ref,
        parent,
        candidate_ref,
        candidate,
    )
    row = _require_one_composite_row(
        parent_ref,
        candidate_ref,
        candidate,
        matches,
    )
    return CanonicalLeafBinding(row=row, source=canonical_leaf_source(parent_ref, row))


def _require_binding_documents(parent: TaskDocument, candidate: TaskDocument) -> None:
    if parent.kind != "master":
        raise CanonicalLeafBindingError(
            "task-leaf-binding-parent-invalid",
            "canonical leaf binding requires a master parent document",
        )
    if candidate.kind != "subTask":
        raise CanonicalLeafBindingError(
            "task-leaf-binding-child-invalid",
            "canonical leaf binding requires a subTask child document",
        )


def _require_direct_child_candidate(
    parent_ref: TaskDocumentRef,
    candidate_ref: TaskDocumentRef,
) -> None:
    parent_path = PurePosixPath(parent_ref.path)
    candidate_path = PurePosixPath(candidate_ref.path)
    if (
        candidate_ref.repository != parent_ref.repository
        or candidate_path.parent != parent_path.parent
    ):
        raise CanonicalLeafBindingError(
            "task-leaf-binding-wrong-directory",
            "candidate leaf is not a direct child of its declared parent directory",
        )


def _candidate_row_matches(
    parent_ref: TaskDocumentRef,
    parent: TaskDocument,
    candidate_ref: TaskDocumentRef,
    candidate: TaskDocument,
) -> _LeafRowMatches:
    candidate_path = PurePosixPath(candidate_ref.path)
    numbered = [
        (index, row) for index, row in enumerate(parent.subTasks) if row.number == candidate.id
    ]
    sourced = _rows_for_candidate_source(parent_ref, parent, candidate_ref)
    stemmed = [
        (index, row)
        for index, row in enumerate(parent.subTasks)
        if row.file and PurePosixPath(row.file).stem == candidate_path.stem
    ]
    return _LeafRowMatches(numbered=numbered, sourced=sourced, stemmed=stemmed)


def _require_one_composite_row(
    parent_ref: TaskDocumentRef,
    candidate_ref: TaskDocumentRef,
    candidate: TaskDocument,
    matches: _LeafRowMatches,
) -> SubTaskRef:
    numbered = matches.numbered
    sourced = matches.sourced
    stemmed = matches.stemmed
    if len(numbered) > 1 or len(sourced) > 1:
        raise CanonicalLeafBindingError(
            "task-leaf-binding-row-ambiguous",
            "candidate leaf resolves to multiple parent row identities",
        )
    if numbered and sourced:
        return _require_same_row_identity(numbered, sourced)
    if numbered:
        return _require_numbered_row_source(parent_ref, candidate_ref, numbered, stemmed)
    elif sourced or stemmed:
        raise CanonicalLeafBindingError(
            "task-leaf-binding-stem-only",
            "candidate source or file stem matches without the candidate document ID",
        )
    raise CanonicalLeafBindingError(
        "task-leaf-binding-row-missing",
        f"parent task contains no composite row binding for leaf {candidate.id!r}",
    )


def _require_same_row_identity(
    numbered: list[tuple[int, SubTaskRef]],
    sourced: list[tuple[int, SubTaskRef]],
) -> SubTaskRef:
    if numbered[0][0] != sourced[0][0]:
        raise CanonicalLeafBindingError(
            "task-leaf-binding-identity-split",
            "row number and row source identify different candidate leaves",
        )
    return numbered[0][1]


def _require_numbered_row_source(
    parent_ref: TaskDocumentRef,
    candidate_ref: TaskDocumentRef,
    numbered: list[tuple[int, SubTaskRef]],
    stemmed: list[tuple[int, SubTaskRef]],
) -> SubTaskRef:
    row_index, row = numbered[0]
    source = canonical_leaf_source(parent_ref, row)
    if any(index != row_index for index, _ in stemmed):
        raise CanonicalLeafBindingError(
            "task-leaf-binding-identity-split",
            "row number and file-stem source identify different candidate leaves",
        )
    if source.json_ref != candidate_ref:
        raise CanonicalLeafBindingError(
            "task-leaf-binding-source-mismatch",
            "the candidate-ID row points at a different canonical child source",
        )
    return row


def _rows_for_candidate_source(
    parent_ref: TaskDocumentRef,
    parent: TaskDocument,
    candidate_ref: TaskDocumentRef,
) -> list[tuple[int, SubTaskRef]]:
    matches: list[tuple[int, SubTaskRef]] = []
    for index, row in enumerate(parent.subTasks):
        try:
            source = canonical_leaf_source(parent_ref, row)
        except CanonicalLeafBindingError:
            continue
        if source.json_ref == candidate_ref:
            matches.append((index, row))
    return matches


__all__ = [
    "CanonicalLeafBinding",
    "CanonicalLeafBindingError",
    "CanonicalLeafSource",
    "canonical_leaf_source",
    "require_canonical_leaf_binding",
    "require_leaf_parent_row",
]
