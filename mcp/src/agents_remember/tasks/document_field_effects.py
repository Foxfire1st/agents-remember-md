"""Exhaustive field-effect taxonomy for persisted task-document schemas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, get_args

from pydantic import BaseModel

from agents_remember.errors import AgentsRememberError
from agents_remember.models.lifecycles.evidence_dependencies import (
    EvidenceDependencies,
    EvidenceDependency,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    AcceptanceObligationQuestion,
    ApprovedRequirementPacketRef,
    MissingTaskIntent,
    TaskIntentIdentity,
)

from .document import (
    CodeExample,
    Decision,
    DiscardedSubTask,
    DiscardSourceProof,
    DiscardUnstartedProof,
    HeaderNote,
    RouteReviewRecord,
    RouteReviewUnit,
    Section,
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
    SprintSeat,
    Step,
    StepDisposition,
    SubStep,
    SubTaskRef,
    TaskDocument,
    TaskEnclosureRef,
    TaskExecutionRegistration,
)


class TaskDocumentFieldEffect(StrEnum):
    """Named projections and state planes a schema field is allowed to affect."""

    STRUCTURAL_TOPOLOGY = "structural-topology"
    NORMATIVE_INTENT = "normative-intent"
    COMPLETION_READINESS = "completion-readiness"
    PROGRESS = "progress"
    EVIDENCE = "evidence"
    LIFECYCLE = "lifecycle"
    PROSE_AUDIT = "prose-audit"


class TaskDocumentFieldEffectTaxonomyError(AgentsRememberError):
    """The persisted schema changed without an explicit effect classification."""


class TaskDocumentMutationClass(StrEnum):
    """Semantic class carried by one exact accepted/candidate task delta."""

    TOPOLOGY = "topology"
    INTENT = "intent"
    COMPLETION_READINESS = "completion-readiness"
    ACCEPTANCE_EVIDENCE = "acceptance-evidence"
    OPERATIONAL_AUDIT = "operational-audit"


class TaskDocumentMutationClassificationError(AgentsRememberError):
    """A task mutation cannot be classified exhaustively before publication."""


@dataclass(frozen=True)
class TaskDocumentMutationClassification:
    """The complete semantic classes for one exact task-document mutation."""

    classes: frozenset[TaskDocumentMutationClass]

    @property
    def invalidates_projection(self) -> bool:
        return bool(
            self.classes
            & {
                TaskDocumentMutationClass.TOPOLOGY,
                TaskDocumentMutationClass.INTENT,
                TaskDocumentMutationClass.COMPLETION_READINESS,
            }
        )


FieldEffects = frozenset[TaskDocumentFieldEffect]


class TaskDocumentFieldEffectProjector:
    """Validate one taxonomy snapshot, then project any validated schema value."""

    def __init__(self) -> None:
        validate_task_document_field_effects()
        self._validated_models: set[type[BaseModel]] = {TaskDocument}

    def project(
        self,
        model: BaseModel,
        effect: TaskDocumentFieldEffect,
    ) -> dict[str, object]:
        self.validate(model)
        return _project_model_field_effect(model, effect)

    def validate(self, model: BaseModel) -> None:
        """Require this exact runtime model schema to be fully classified."""

        model_type = type(model)
        if model_type not in self._validated_models:
            validate_task_document_field_effects(root_model=model_type)
            self._validated_models.add(model_type)


def _effects(*effects: TaskDocumentFieldEffect) -> FieldEffects:
    return frozenset(effects)


STRUCTURAL = _effects(TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY)
NORMATIVE = _effects(TaskDocumentFieldEffect.NORMATIVE_INTENT)
READINESS = _effects(TaskDocumentFieldEffect.COMPLETION_READINESS)
PROGRESS = _effects(TaskDocumentFieldEffect.PROGRESS)
EVIDENCE = _effects(TaskDocumentFieldEffect.EVIDENCE)
LIFECYCLE = _effects(TaskDocumentFieldEffect.LIFECYCLE)
AUDIT = _effects(TaskDocumentFieldEffect.PROSE_AUDIT)
INTENT_AND_PROGRESS = NORMATIVE | PROGRESS
PROGRESS_AND_READINESS = PROGRESS | READINESS


TASK_DOCUMENT_FIELD_EFFECTS: dict[type[BaseModel], dict[str, FieldEffects]] = {
    TaskDocument: {
        "schema_": AUDIT,
        "id": NORMATIVE | STRUCTURAL,
        "slug": NORMATIVE,
        "title": NORMATIVE,
        "kind": NORMATIVE | STRUCTURAL,
        "status": PROGRESS_AND_READINESS,
        "statusNote": AUDIT,
        "repo": NORMATIVE,
        "type": NORMATIVE,
        "createdAt": AUDIT,
        "master": AUDIT,
        "headerNotes": AUDIT,
        "seriesContractPath": LIFECYCLE,
        "integrationBranch": LIFECYCLE,
        "executionNature": STRUCTURAL,
        "executionGraph": STRUCTURAL,
        "enclosures": LIFECYCLE,
        "executionRegistrations": LIFECYCLE,
        "lifecycleId": LIFECYCLE,
        "objective": NORMATIVE,
        "requirements": NORMATIVE,
        "design": NORMATIVE,
        "steps": INTENT_AND_PROGRESS | READINESS,
        "codeExamples": NORMATIVE,
        "codeExamplesNote": NORMATIVE,
        "decisions": AUDIT,
        "routeReview": EVIDENCE,
        "openQuestions": NORMATIVE,
        "references": EVIDENCE,
        "subTasks": STRUCTURAL | PROGRESS_AND_READINESS,
        "discardedSubTasks": AUDIT,
        "sections": AUDIT,
        "orchestrates": STRUCTURAL,
        "seats": LIFECYCLE,
    },
    StepDisposition: {
        "kind": AUDIT,
        "reason": AUDIT,
        "recordedAt": AUDIT,
        "recordedVia": AUDIT,
        "lifecycleId": LIFECYCLE,
    },
    SubStep: {
        "id": NORMATIVE,
        "title": NORMATIVE,
        "status": PROGRESS_AND_READINESS,
        "note": AUDIT,
        "disposition": AUDIT,
    },
    Step: {
        "id": NORMATIVE,
        "title": NORMATIVE,
        "outcome": NORMATIVE,
        "status": PROGRESS_AND_READINESS,
        "substeps": INTENT_AND_PROGRESS | READINESS,
        "disposition": AUDIT,
    },
    Decision: {"at": AUDIT, "decision": AUDIT, "rationale": AUDIT},
    RouteReviewUnit: {
        "route": EVIDENCE,
        "verdict": EVIDENCE,
        "evidenceRef": EVIDENCE,
        "evidenceSha256": EVIDENCE,
    },
    RouteReviewRecord: {
        "candidateTree": EVIDENCE,
        "verdict": EVIDENCE,
        "verdictRef": EVIDENCE,
        "reviewedAt": EVIDENCE,
        "routes": EVIDENCE,
        "taskIntent": EVIDENCE,
        "verdictSha256": EVIDENCE,
        "dependencies": EVIDENCE,
        "recordDigest": EVIDENCE,
    },
    EvidenceDependencies: {
        "schemaVersion": EVIDENCE,
        "recordType": EVIDENCE,
        "validatorVersion": EVIDENCE,
        "edges": EVIDENCE,
    },
    EvidenceDependency: {
        "kind": EVIDENCE,
        "name": EVIDENCE,
        "algorithm": EVIDENCE,
        "digest": EVIDENCE,
    },
    CodeExample: {
        "id": NORMATIVE,
        "title": NORMATIVE,
        "distinctChange": NORMATIVE,
        "why": NORMATIVE,
        "language": NORMATIVE,
        "snippet": NORMATIVE,
    },
    HeaderNote: {"label": AUDIT, "value": AUDIT},
    TaskEnclosureRef: {"leafId": LIFECYCLE, "enclosurePath": LIFECYCLE},
    TaskExecutionRegistration: {
        "sourceKind": LIFECYCLE,
        "role": LIFECYCLE,
        "sourceId": LIFECYCLE,
        "observedAt": LIFECYCLE,
    },
    SprintExecutionEndpoint: {"ref": STRUCTURAL, "leafId": STRUCTURAL},
    SprintExecutionNode: {"kind": STRUCTURAL, "ref": STRUCTURAL, "leafIds": STRUCTURAL},
    SprintExecutionEdge: {
        "predecessor": STRUCTURAL,
        "successor": STRUCTURAL,
        "reason": NORMATIVE,
        "judgmentId": EVIDENCE,
    },
    SprintExecutionGraph: {"nodes": STRUCTURAL, "edges": STRUCTURAL},
    SubTaskRef: {
        "number": STRUCTURAL,
        "name": NORMATIVE,
        "file": STRUCTURAL,
        "status": PROGRESS_AND_READINESS,
        "scope": STRUCTURAL,
        "masterRef": STRUCTURAL,
    },
    DiscardSourceProof: {"state": AUDIT, "sha256": AUDIT, "size": AUDIT},
    DiscardUnstartedProof: {
        "version": AUDIT,
        "taskDocumentRef": AUDIT,
        "taskState": AUDIT,
        "enclosureState": AUDIT,
        "locatorState": AUDIT,
        "doorState": AUDIT,
        "operationState": AUDIT,
        "seatState": AUDIT,
        "reviewState": AUDIT,
        "commitState": AUDIT,
        "childJson": AUDIT,
        "childMarkdown": AUDIT,
        "fingerprint": AUDIT,
    },
    DiscardedSubTask: {
        "number": AUDIT,
        "name": AUDIT,
        "file": AUDIT,
        "scope": AUDIT,
        "disposition": AUDIT,
        "reason": AUDIT,
        "discardedAt": AUDIT,
        "proof": AUDIT,
    },
    SprintSeat: {"role": LIFECYCLE, "label": LIFECYCLE, "identity": LIFECYCLE, "state": LIFECYCLE},
    Section: {"kind": AUDIT, "heading": AUDIT, "body": AUDIT},
    ApprovedRequirementPacketRef: {
        "kind": NORMATIVE,
        "path": NORMATIVE,
        "stableId": NORMATIVE,
        "version": NORMATIVE,
    },
    AcceptanceObligationQuestion: {"kind": NORMATIVE, "id": NORMATIVE, "question": NORMATIVE},
    TaskIntentIdentity: {"schema_": EVIDENCE, "digest": EVIDENCE},
    MissingTaskIntent: {"state": EVIDENCE},
    TaskDocumentRef: {
        "repository": NORMATIVE | STRUCTURAL,
        "path": NORMATIVE | STRUCTURAL,
    },
}


FIELD_EFFECT_MUTATION_CLASSES: dict[TaskDocumentFieldEffect, TaskDocumentMutationClass] = {
    TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY: TaskDocumentMutationClass.TOPOLOGY,
    TaskDocumentFieldEffect.NORMATIVE_INTENT: TaskDocumentMutationClass.INTENT,
    TaskDocumentFieldEffect.COMPLETION_READINESS: (TaskDocumentMutationClass.COMPLETION_READINESS),
    TaskDocumentFieldEffect.PROGRESS: TaskDocumentMutationClass.COMPLETION_READINESS,
    TaskDocumentFieldEffect.EVIDENCE: TaskDocumentMutationClass.ACCEPTANCE_EVIDENCE,
    TaskDocumentFieldEffect.LIFECYCLE: TaskDocumentMutationClass.OPERATIONAL_AUDIT,
    TaskDocumentFieldEffect.PROSE_AUDIT: TaskDocumentMutationClass.OPERATIONAL_AUDIT,
}


def classify_task_document_mutation(
    original: TaskDocument | None,
    candidate: TaskDocument,
) -> TaskDocumentMutationClassification:
    """Classify the exact accepted/candidate field delta through the canonical taxonomy."""

    validate_task_document_field_effects(root_model=type(candidate))
    if original is not None:
        validate_task_document_field_effects(root_model=type(original))
    validate_task_document_mutation_classes()
    effects = _changed_value_effects(original, candidate, frozenset())
    return TaskDocumentMutationClassification(
        frozenset(FIELD_EFFECT_MUTATION_CLASSES[effect] for effect in effects)
    )


def validate_task_document_mutation_classes() -> None:
    """Refuse an effect vocabulary change without a corresponding mutation class."""

    missing = set(TaskDocumentFieldEffect).difference(FIELD_EFFECT_MUTATION_CLASSES)
    stale = set(FIELD_EFFECT_MUTATION_CLASSES).difference(TaskDocumentFieldEffect)
    if missing or stale:
        raise TaskDocumentMutationClassificationError(
            "invalid task-document mutation-class mapping: "
            f"missing={sorted(effect.value for effect in missing)}, "
            f"stale={sorted(str(effect) for effect in stale)}"
        )


def _changed_model_effects(original: BaseModel, candidate: BaseModel) -> FieldEffects:
    if type(original) is not type(candidate):
        raise TaskDocumentMutationClassificationError(
            "task-document mutation changed persisted model type: "
            f"{_model_name(type(original))} -> {_model_name(type(candidate))}"
        )
    classifications = _classifications_for(type(candidate))
    if classifications is None:
        raise TaskDocumentMutationClassificationError(
            f"unclassified task-document schema model: {_model_name(type(candidate))}"
        )
    effects: set[TaskDocumentFieldEffect] = set()
    for name in type(candidate).model_fields:
        effects.update(
            _changed_value_effects(
                getattr(original, name),
                getattr(candidate, name),
                classifications[name],
            )
        )
    return frozenset(effects)


def _changed_value_effects(
    original: object,
    candidate: object,
    owner_effects: FieldEffects,
) -> FieldEffects:
    if original == candidate:
        return frozenset()
    if isinstance(original, BaseModel) and isinstance(candidate, BaseModel):
        return _changed_model_value_effects(original, candidate, owner_effects)
    if isinstance(original, Mapping) and isinstance(candidate, Mapping):
        return _changed_mapping_effects(original, candidate, owner_effects)
    if isinstance(original, (list, tuple)) and isinstance(candidate, (list, tuple)):
        effects = {
            effect
            for before, after in zip(original, candidate, strict=False)
            for effect in _changed_value_effects(before, after, owner_effects)
        }
        if len(original) != len(candidate):
            effects.update(owner_effects)
            effects.update(_present_value_effects(original[len(candidate) :]))
            effects.update(_present_value_effects(candidate[len(original) :]))
        return frozenset(effects)
    if original is None or candidate is None:
        present = candidate if original is None else original
        return owner_effects | _present_value_effects(present)
    return owner_effects


def _changed_model_value_effects(
    original: BaseModel,
    candidate: BaseModel,
    owner_effects: FieldEffects,
) -> FieldEffects:
    if type(original) is type(candidate):
        return _changed_model_effects(original, candidate)
    return owner_effects | _present_value_effects(original) | _present_value_effects(candidate)


def _changed_mapping_effects(
    original: Mapping[object, object],
    candidate: Mapping[object, object],
    owner_effects: FieldEffects,
) -> FieldEffects:
    if set(original) != set(candidate):
        return owner_effects | _present_value_effects(original) | _present_value_effects(candidate)
    return frozenset(
        effect
        for key in original
        for effect in _changed_value_effects(original[key], candidate[key], owner_effects)
    )


def _present_value_effects(value: object) -> FieldEffects:
    if isinstance(value, BaseModel):
        classifications = _classifications_for(type(value))
        if classifications is None:
            raise TaskDocumentMutationClassificationError(
                f"unclassified task-document schema model: {_model_name(type(value))}"
            )
        return frozenset(
            effect
            for name, field_effects in classifications.items()
            for effect in field_effects | _present_value_effects(getattr(value, name))
        )
    if isinstance(value, Mapping):
        return frozenset(
            effect for item in value.values() for effect in _present_value_effects(item)
        )
    if isinstance(value, (list, tuple)):
        return frozenset(effect for item in value for effect in _present_value_effects(item))
    return frozenset()


def task_document_schema_models(
    root_model: type[BaseModel] = TaskDocument,
) -> frozenset[type[BaseModel]]:
    """Return every persisted Pydantic model reachable from a task document."""

    pending = [root_model]
    models: set[type[BaseModel]] = set()
    while pending:
        model = pending.pop()
        if model in models:
            continue
        models.add(model)
        for field in model.model_fields.values():
            pending.extend(_nested_models(field.annotation))
    return frozenset(models)


def validate_task_document_field_effects(
    *,
    root_model: type[BaseModel] = TaskDocument,
) -> None:
    """Refuse any unclassified, stale, or empty task-document field taxonomy entry."""

    for model in task_document_schema_models(root_model):
        classifications = _classifications_for(model)
        if classifications is None:
            raise TaskDocumentFieldEffectTaxonomyError(
                f"unclassified task-document schema model: {_model_name(model)}"
            )
        if detail := _taxonomy_difference(model, classifications):
            raise TaskDocumentFieldEffectTaxonomyError(
                f"invalid field-effect taxonomy for {_model_name(model)}: {detail}"
            )


def _taxonomy_difference(
    model: type[BaseModel],
    classifications: Mapping[str, FieldEffects],
) -> str:
    actual = set(model.model_fields)
    differences = {
        "missing": sorted(actual.difference(classifications)),
        "stale": sorted(set(classifications).difference(actual)),
        "empty": sorted(name for name, effects in classifications.items() if not effects),
    }
    return ", ".join(f"{name}={values}" for name, values in differences.items() if values)


def fields_with_effect(model: type[BaseModel], effect: TaskDocumentFieldEffect) -> frozenset[str]:
    """Return a model's explicitly classified members after exhaustiveness validation."""

    validate_task_document_field_effects()
    validate_task_document_field_effects(root_model=model)
    classifications = _classifications_for(model)
    if classifications is None:
        raise TaskDocumentFieldEffectTaxonomyError(
            f"unclassified task-document schema model: {_model_name(model)}"
        )
    return frozenset(name for name, effects in classifications.items() if effect in effects)


def project_model_field_effect(
    model: BaseModel,
    effect: TaskDocumentFieldEffect,
) -> dict[str, object]:
    """Project one persisted schema value through the canonical effect taxonomy.

    Nested models are filtered recursively.  Consumers therefore select an effect
    plane without restating model or field membership, and a schema change is
    refused by the exhaustive validator before any projection is returned.
    """

    return TaskDocumentFieldEffectProjector().project(model, effect)


def _project_model_field_effect(
    model: BaseModel,
    effect: TaskDocumentFieldEffect,
) -> dict[str, object]:
    classifications = _classifications_for(type(model))
    if classifications is None:
        raise TaskDocumentFieldEffectTaxonomyError(
            f"unclassified task-document schema model: {_model_name(type(model))}"
        )
    projected: dict[str, object] = {}
    for name, field in type(model).model_fields.items():
        if effect not in classifications[name]:
            continue
        alias = field.serialization_alias or field.alias or name
        projected[alias] = _project_field_value(getattr(model, name), effect)
    return projected


def _project_field_value(value: object, effect: TaskDocumentFieldEffect) -> object:
    if isinstance(value, BaseModel):
        return _project_model_field_effect(value, effect)
    if isinstance(value, Mapping):
        return {
            str(key): _project_field_value(item, effect)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_project_field_value(item, effect) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _classifications_for(model: type[BaseModel]) -> Mapping[str, FieldEffects] | None:
    for candidate in model.__mro__:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            classification = TASK_DOCUMENT_FIELD_EFFECTS.get(candidate)
            if classification is not None:
                return classification
    return None


def _nested_models(annotation: Any) -> Iterable[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    return tuple(model for arg in get_args(annotation) for model in _nested_models(arg))


def _model_name(model: type[BaseModel]) -> str:
    return f"{model.__module__}.{model.__qualname__}"


__all__ = [
    "FIELD_EFFECT_MUTATION_CLASSES",
    "TASK_DOCUMENT_FIELD_EFFECTS",
    "TaskDocumentFieldEffect",
    "TaskDocumentFieldEffectProjector",
    "TaskDocumentFieldEffectTaxonomyError",
    "TaskDocumentMutationClass",
    "TaskDocumentMutationClassification",
    "TaskDocumentMutationClassificationError",
    "classify_task_document_mutation",
    "fields_with_effect",
    "project_model_field_effect",
    "task_document_schema_models",
    "validate_task_document_field_effects",
    "validate_task_document_mutation_classes",
]
