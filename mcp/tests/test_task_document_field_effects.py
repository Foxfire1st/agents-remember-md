"""Exhaustiveness forcing for the canonical task-document field taxonomy."""

from __future__ import annotations

import pytest
from agents_remember.tasks.document import HeaderNote, Section, TaskDocument
from agents_remember.tasks.document_field_effects import (
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectTaxonomyError,
    project_model_field_effect,
    validate_task_document_field_effects,
)
from pydantic import Field


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
