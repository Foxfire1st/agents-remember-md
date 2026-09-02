"""Exhaustiveness forcing for the canonical task-document field taxonomy."""

from __future__ import annotations

import re

import pytest
from agents_remember.models.task_intent import MissingTaskIntent, TaskIntentIdentity
from agents_remember.tasks.document import (
    HeaderNote,
    Section,
    Step,
    StepDisposition,
    TaskDocument,
)
from agents_remember.tasks.document_field_effects import (
    FIELD_EFFECT_MUTATION_CLASSES,
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectTaxonomyError,
    TaskDocumentMutationClass,
    TaskDocumentMutationClassificationError,
    _changed_mapping_effects,
    _changed_model_effects,
    _changed_model_value_effects,
    _changed_value_effects,
    _present_value_effects,
    classify_task_document_mutation,
    project_model_field_effect,
    validate_task_document_field_effects,
    validate_task_document_mutation_classes,
)
from pydantic import BaseModel, Field


def test_taxonomy_covers_current_schema_and_refuses_a_future_nested_field() -> None:
    validate_task_document_field_effects()

    class FutureHeaderNote(HeaderNote):
        futureNestedFact: str = "unclassified"

    class FutureTaskDocument(TaskDocument):
        headerNotes: list[FutureHeaderNote] = Field(default_factory=list)

    with pytest.raises(TaskDocumentFieldEffectTaxonomyError, match="futureNestedFact"):
        validate_task_document_field_effects(root_model=FutureTaskDocument)


def test_leaf_identity_shape_stays_normative_and_is_explicitly_structural() -> None:
    for field in ("id", "kind"):
        effects = TASK_DOCUMENT_FIELD_EFFECTS[TaskDocument][field]
        assert TaskDocumentFieldEffect.NORMATIVE_INTENT in effects
        assert TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects


def test_freeform_section_bytes_are_audit_only_and_never_normative() -> None:
    section = Section(
        kind="freeform",
        heading="Exact heading — keep bytes",
        body="Line one.\n\nLine two with `syntax`.",
    )
    document = TaskDocument.model_validate(
        {
            "id": "L1",
            "slug": "leaf",
            "title": "Leaf",
            "kind": "subTask",
            "repo": "repo",
            "createdAt": "2026-09-01T00:00:00+00:00",
            "sections": [section.model_dump(mode="json")],
        }
    )

    audit = project_model_field_effect(document, TaskDocumentFieldEffect.PROSE_AUDIT)
    normative = project_model_field_effect(document, TaskDocumentFieldEffect.NORMATIVE_INTENT)

    assert audit["sections"] == [section.model_dump(mode="json")]
    assert "sections" not in normative
    for field in ("kind", "heading", "body"):
        assert TASK_DOCUMENT_FIELD_EFFECTS[Section][field] == frozenset(
            {TaskDocumentFieldEffect.PROSE_AUDIT}
        )


def test_taxonomy_refuses_stale_and_empty_memberships(monkeypatch: pytest.MonkeyPatch) -> None:
    original = TASK_DOCUMENT_FIELD_EFFECTS[HeaderNote]
    stale = {**original, "retiredField": original["label"]}
    monkeypatch.setitem(TASK_DOCUMENT_FIELD_EFFECTS, HeaderNote, stale)
    with pytest.raises(TaskDocumentFieldEffectTaxonomyError, match=r"stale=.*retiredField"):
        validate_task_document_field_effects()

    empty = {**original, "label": frozenset()}
    monkeypatch.setitem(TASK_DOCUMENT_FIELD_EFFECTS, HeaderNote, empty)
    with pytest.raises(TaskDocumentFieldEffectTaxonomyError, match=r"empty=.*label"):
        validate_task_document_field_effects()


def _mutation_document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "MASTER",
            "slug": "leaf",
            "title": "Leaf",
            "kind": "subTask",
            "status": "inProgress",
            "repo": "repo",
            "type": "Code",
            "createdAt": "2026-09-01T00:00:00+00:00",
            "steps": [{"id": "S1", "title": "Deliver", "status": "pending"}],
        }
    )


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"id": "RENAMED"},
            {TaskDocumentMutationClass.TOPOLOGY, TaskDocumentMutationClass.INTENT},
        ),
        ({"title": "Renamed"}, {TaskDocumentMutationClass.INTENT}),
        (
            {"status": "Completed"},
            {TaskDocumentMutationClass.COMPLETION_READINESS},
        ),
        (
            {"references": ["requirements/R04.md"]},
            {TaskDocumentMutationClass.ACCEPTANCE_EVIDENCE},
        ),
        (
            {"statusNote": "Candidate prepared"},
            {TaskDocumentMutationClass.OPERATIONAL_AUDIT},
        ),
    ],
)
def test_exact_top_level_delta_maps_to_canonical_mutation_classes(
    updates: dict[str, object],
    expected: set[TaskDocumentMutationClass],
) -> None:
    original = _mutation_document()
    candidate = original.model_copy(update=updates)

    classification = classify_task_document_mutation(original, candidate)

    assert classification.classes == expected
    assert classification.invalidates_projection is bool(
        expected
        & {
            TaskDocumentMutationClass.TOPOLOGY,
            TaskDocumentMutationClass.INTENT,
            TaskDocumentMutationClass.COMPLETION_READINESS,
        }
    )


def test_nested_delta_uses_changed_leaf_field_instead_of_container_name() -> None:
    original = _mutation_document()
    original_step = original.steps[0]
    completed_step = original_step.model_copy(update={"status": "done"})
    audit_step = Step(
        id=original_step.id,
        title=original_step.title,
        status="done",
        disposition=StepDisposition(
            reason="Superseded by exact proof.",
            recordedAt="2026-09-02T00:00:00+00:00",
        ),
    )

    audit = classify_task_document_mutation(
        original.model_copy(update={"steps": [completed_step]}),
        original.model_copy(update={"steps": [audit_step]}),
    )
    completion = classify_task_document_mutation(
        original,
        original.model_copy(update={"steps": [completed_step]}),
    )

    assert audit.classes == {TaskDocumentMutationClass.OPERATIONAL_AUDIT}
    assert not audit.invalidates_projection
    assert completion.classes == {TaskDocumentMutationClass.COMPLETION_READINESS}
    assert completion.invalidates_projection


def test_new_document_and_effect_mapping_are_exhaustive() -> None:
    validate_task_document_mutation_classes()
    classification = classify_task_document_mutation(None, _mutation_document())

    assert set(FIELD_EFFECT_MUTATION_CLASSES) == set(TaskDocumentFieldEffect)
    assert set(FIELD_EFFECT_MUTATION_CLASSES.values()) == set(TaskDocumentMutationClass)
    assert classification.classes == set(TaskDocumentMutationClass)
    assert classification.invalidates_projection


def test_missing_effect_to_mutation_mapping_refuses_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(
        FIELD_EFFECT_MUTATION_CLASSES,
        TaskDocumentFieldEffect.PROSE_AUDIT,
    )

    with pytest.raises(TaskDocumentMutationClassificationError, match=r"missing=.*prose-audit"):
        classify_task_document_mutation(_mutation_document(), _mutation_document())


def test_nested_model_refusals_cover_unknown_and_incompatible_schema_types() -> None:
    class UnclassifiedModel(BaseModel):
        value: str

    with pytest.raises(
        TaskDocumentMutationClassificationError,
        match=(
            r"persisted model type: agents_remember\.tasks\.document\.HeaderNote -> "
            r"agents_remember\.tasks\.document\.Section"
        ),
    ):
        _changed_model_effects(
            HeaderNote(label="before", value="value"),
            Section(heading="after"),
        )

    before = UnclassifiedModel(value="before")
    after = UnclassifiedModel(value="after")
    unclassified_model_identity = re.escape(
        f"{UnclassifiedModel.__module__}.{UnclassifiedModel.__qualname__}"
    )
    with pytest.raises(
        TaskDocumentMutationClassificationError,
        match=rf"unclassified task-document schema model: {unclassified_model_identity}",
    ):
        _changed_model_effects(before, after)
    with pytest.raises(
        TaskDocumentMutationClassificationError,
        match=rf"unclassified task-document schema model: {unclassified_model_identity}",
    ):
        _present_value_effects(before)


def test_nested_mapping_and_model_union_variants_preserve_all_owned_effects() -> None:
    audit = frozenset({TaskDocumentFieldEffect.PROSE_AUDIT})
    evidence = frozenset({TaskDocumentFieldEffect.EVIDENCE})
    before_note = HeaderNote(label="note", value="before")
    after_note = HeaderNote(label="note", value="after")

    assert (
        _changed_value_effects(
            {"same": before_note},
            {"same": after_note},
            audit,
        )
        == audit
    )
    assert (
        _changed_mapping_effects(
            {"removed": before_note},
            {"added": after_note},
            evidence,
        )
        == audit | evidence
    )
    assert (
        _changed_model_value_effects(
            MissingTaskIntent(),
            TaskIntentIdentity(digest="a" * 64),
            evidence,
        )
        == evidence
    )
