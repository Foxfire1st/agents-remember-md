"""Canonical normative task-intent identity for one leaf task document."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents_remember.errors import TaskIntentError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    TASK_INTENT_SCHEMA,
    AcceptanceObligationQuestion,
    ApprovedRequirementPacketRef,
    TaskIntentIdentity,
    TaskIntentState,
    require_task_intent_identity,
)

from .document import CodeExample, Step, SubStep, TaskDocument
from .document_field_effects import (
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectProjector,
    TaskDocumentFieldEffectTaxonomyError,
    fields_with_effect,
)
from .document_refs import ResolvedTaskDocument


class _StrictProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskIntentLeafIdentity(_StrictProjection):
    ref: TaskDocumentRef
    id: str
    slug: str
    title: str
    kind: Literal["light", "subTask"]
    repo: str
    type: str


class TaskIntentRequirementText(_StrictProjection):
    kind: Literal["exact-text"] = "exact-text"
    text: str


class TaskIntentRequirementPacket(_StrictProjection):
    kind: Literal["approved-requirement-packet"] = "approved-requirement-packet"
    path: str
    stableId: str
    version: str


TaskIntentRequirement = TaskIntentRequirementText | TaskIntentRequirementPacket


class TaskIntentSubStep(_StrictProjection):
    id: str
    title: str


class TaskIntentStep(_StrictProjection):
    id: str
    title: str
    outcome: str | None
    substeps: tuple[TaskIntentSubStep, ...]


class TaskIntentCodeExample(_StrictProjection):
    id: str
    title: str
    distinctChange: str
    why: str
    language: str
    snippet: str


class TaskIntentAcceptanceObligation(_StrictProjection):
    id: str
    question: str


class TaskIntentV1(_StrictProjection):
    """Only explicitly allowlisted normative slots; never a whole-document hash."""

    schema_: Literal["task-intent/v1"] = Field(default=TASK_INTENT_SCHEMA, alias="schema")
    leaf: TaskIntentLeafIdentity
    objective: str
    requirements: tuple[TaskIntentRequirement, ...]
    design: str | None
    steps: tuple[TaskIntentStep, ...]
    codeExamples: tuple[TaskIntentCodeExample, ...]
    codeExamplesNote: str | None
    acceptanceObligations: tuple[TaskIntentAcceptanceObligation, ...]

    def canonical_value(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=False)


_ROOT_FIELDS = frozenset(
    {
        "id",
        "slug",
        "title",
        "kind",
        "repo",
        "type",
        "objective",
        "requirements",
        "design",
        "steps",
        "codeExamples",
        "codeExamplesNote",
        "openQuestions",
    }
)
_NESTED_FIELDS: dict[type[BaseModel], frozenset[str]] = {
    Step: frozenset({"id", "title", "outcome", "substeps"}),
    SubStep: frozenset({"id", "title"}),
    CodeExample: frozenset({"id", "title", "distinctChange", "why", "language", "snippet"}),
    ApprovedRequirementPacketRef: frozenset({"kind", "path", "stableId", "version"}),
    AcceptanceObligationQuestion: frozenset({"kind", "id", "question"}),
    TaskDocumentRef: frozenset({"repository", "path"}),
}


def task_intent_projection(
    task_root: Path,
    candidate: ResolvedTaskDocument,
    *,
    schema_version: str = TASK_INTENT_SCHEMA,
) -> TaskIntentV1:
    """Project one leaf through the shared exhaustive field taxonomy."""

    _require_schema_version(schema_version)
    if candidate.document.kind == "master":
        raise TaskIntentError(
            "task-intent-leaf-required",
            "normative task intent belongs to a light or subTask leaf document",
        )
    _validate_allowlisted_classifications()
    projected = _normative_projection(candidate.document)
    try:
        return TaskIntentV1(
            leaf=TaskIntentLeafIdentity(
                ref=candidate.ref,
                id=candidate.document.id,
                slug=candidate.document.slug,
                title=candidate.document.title,
                kind=candidate.document.kind,
                repo=candidate.document.repo,
                type=candidate.document.type,
            ),
            objective=candidate.document.objective,
            requirements=_requirements(task_root, candidate.document),
            design=candidate.document.design,
            steps=tuple(
                TaskIntentStep.model_validate(value)
                for value in cast(list[object], projected["steps"])
            ),
            codeExamples=tuple(
                TaskIntentCodeExample.model_validate(value)
                for value in cast(list[object], projected["codeExamples"])
            ),
            codeExamplesNote=candidate.document.codeExamplesNote,
            acceptanceObligations=_acceptance_obligations(candidate.document),
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise TaskIntentError(
            "task-intent-schema-unclassified",
            "the normative field projection is not fully consumed by task-intent/v1",
        ) from exc


def task_intent_identity(
    task_root: Path,
    candidate: ResolvedTaskDocument,
    *,
    schema_version: str = TASK_INTENT_SCHEMA,
) -> TaskIntentIdentity:
    projection = task_intent_projection(task_root, candidate, schema_version=schema_version)
    encoded = json.dumps(
        projection.canonical_value(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return TaskIntentIdentity(digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def require_current_task_intent(
    observed: TaskIntentState | None,
    current: TaskIntentIdentity,
    *,
    owner: str,
    next_action: str,
) -> TaskIntentIdentity:
    accepted = require_task_intent_identity(
        observed,
        owner=owner,
        next_action=next_action,
    )
    if accepted != current:
        raise TaskIntentError(
            f"{owner}-task-intent-stale",
            f"{owner} binds a different normative task intent",
            next_action=next_action,
        )
    return accepted


def task_intent_fact(identity: TaskIntentIdentity) -> dict[str, str]:
    return identity.model_dump(mode="json", by_alias=True)


def _require_schema_version(schema_version: str) -> None:
    if schema_version != TASK_INTENT_SCHEMA:
        raise TaskIntentError(
            "task-intent-schema-unsupported",
            f"unsupported task intent schema {schema_version!r}",
        )


def _normative_projection(document: TaskDocument) -> dict[str, object]:
    try:
        return TaskDocumentFieldEffectProjector().project(
            document,
            TaskDocumentFieldEffect.NORMATIVE_INTENT,
        )
    except TaskDocumentFieldEffectTaxonomyError as exc:
        raise TaskIntentError("task-intent-schema-unclassified", str(exc)) from exc


def _validate_allowlisted_classifications() -> None:
    required = {TaskDocument: _ROOT_FIELDS, **_NESTED_FIELDS}
    for model, names in required.items():
        try:
            classified = fields_with_effect(model, TaskDocumentFieldEffect.NORMATIVE_INTENT)
        except TaskDocumentFieldEffectTaxonomyError as exc:
            raise TaskIntentError("task-intent-schema-unclassified", str(exc)) from exc
        if missing := names.difference(classified):
            raise TaskIntentError(
                "task-intent-schema-unclassified",
                f"task-intent/v1 slot is not classified normative: {model.__name__}.{sorted(missing)}",
            )
        if unsupported := classified.difference(names):
            raise TaskIntentError(
                "task-intent-schema-unclassified",
                "shared taxonomy contains a normative slot outside task-intent/v1: "
                f"{model.__name__}.{sorted(unsupported)}",
            )


def _requirements(task_root: Path, document: TaskDocument) -> tuple[TaskIntentRequirement, ...]:
    exact = [value for value in document.requirements if isinstance(value, str)]
    if any(not value.strip() for value in exact):
        raise TaskIntentError(
            "task-intent-requirement-text-invalid",
            "task-intent/v1 exact requirement text cannot be blank",
        )
    refs = [
        value for value in document.requirements if isinstance(value, ApprovedRequirementPacketRef)
    ]
    if refs and not exact:
        raise TaskIntentError(
            "task-intent/v2-cutover-required",
            "task-intent/v1 packet references are supplemental and cannot replace exact task text",
        )
    projected: list[TaskIntentRequirement] = []
    for value in document.requirements:
        if isinstance(value, str):
            projected.append(TaskIntentRequirementText(text=value))
        else:
            projected.append(_approved_packet_ref(task_root, value))
    return tuple(projected)


def _approved_packet_ref(
    task_root: Path,
    reference: ApprovedRequirementPacketRef,
) -> TaskIntentRequirementPacket:
    root = task_root.resolve()
    supplied = Path(reference.path)
    resolved = (root / supplied).resolve(strict=False)
    if supplied.is_absolute() or not resolved.is_relative_to(root) or resolved.suffix != ".md":
        raise TaskIntentError(
            "task-intent-requirement-packet-outside-task",
            f"requirement packet must be one task-relative Markdown file: {reference.path}",
        )
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskIntentError(
            "task-intent-requirement-packet-missing",
            f"approved requirement packet is absent or unreadable: {reference.path}",
        ) from exc
    metadata = _packet_metadata(text, packet_path=reference.path)
    identifiers = {metadata[key] for key in ("Stable ID", "Requirement ID") if key in metadata}
    if identifiers != {reference.stableId} or metadata.get("Version") != reference.version:
        raise TaskIntentError(
            "task-intent-requirement-packet-version-mismatch",
            f"requirement packet identity does not match {reference.stableId}@{reference.version}",
        )
    return TaskIntentRequirementPacket(
        path=resolved.relative_to(root).as_posix(),
        stableId=reference.stableId,
        version=reference.version,
    )


def _packet_metadata(text: str, *, packet_path: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] in {"Field", "-----", "---"}:
            continue
        if cells[0] in metadata:
            raise TaskIntentError(
                "task-intent-requirement-packet-metadata-ambiguous",
                f"requirement packet repeats structured field {cells[0]!r}: {packet_path}",
            )
        value = cells[1]
        metadata[cells[0]] = value[1:-1] if value.startswith("`") and value.endswith("`") else value
    return metadata


def _acceptance_obligations(
    document: TaskDocument,
) -> tuple[TaskIntentAcceptanceObligation, ...]:
    return tuple(
        TaskIntentAcceptanceObligation(id=value.id, question=value.question)
        for value in document.openQuestions
        if isinstance(value, AcceptanceObligationQuestion)
    )


__all__ = [
    "TaskIntentAcceptanceObligation",
    "TaskIntentCodeExample",
    "TaskIntentError",
    "TaskIntentLeafIdentity",
    "TaskIntentRequirementPacket",
    "TaskIntentRequirementText",
    "TaskIntentStep",
    "TaskIntentSubStep",
    "TaskIntentV1",
    "require_current_task_intent",
    "task_intent_fact",
    "task_intent_identity",
    "task_intent_projection",
]
