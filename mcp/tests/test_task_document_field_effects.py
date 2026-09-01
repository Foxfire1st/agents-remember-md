"""Exhaustiveness forcing for the canonical task-document field taxonomy."""

from __future__ import annotations

import pytest
from agents_remember.tasks.document import HeaderNote, TaskDocument
from agents_remember.tasks.document_field_effects import (
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectTaxonomyError,
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
