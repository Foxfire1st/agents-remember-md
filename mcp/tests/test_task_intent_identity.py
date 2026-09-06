"""Canonical task-intent projection, mutation, and packet-ref matrices."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents_remember.errors import TaskIntentError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    ApprovedRequirementPacketRef,
    StepDisposition,
    SubStep,
    TaskDocument,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.task_intent import (
    task_intent_identity,
    task_intent_projection,
)


def _document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "L1",
            "slug": "01_leaf",
            "title": "Intent leaf",
            "kind": "subTask",
            "repo": "repo",
            "type": "implementation",
            "createdAt": "2026-09-01T00:00:00+00:00",
            "objective": "Deliver the exact obligation.",
            "requirements": ["The implementation must preserve exact task intent."],
            "design": "Use one canonical projection.",
            "steps": [
                {
                    "id": "S1",
                    "title": "Implement",
                    "outcome": "Intent is bound.",
                    "substeps": [{"id": "S1.1", "title": "Project exact slots"}],
                }
            ],
            "codeExamples": [
                {
                    "id": "E1",
                    "title": "Identity",
                    "distinctChange": "Add a digest.",
                    "why": "Bind meaning.",
                    "language": "python",
                    "snippet": "identity = project(task)",
                }
            ],
            "openQuestions": [
                "Ordinary planning question",
                {
                    "kind": "acceptance-obligation",
                    "id": "A1",
                    "question": "Does every consumer bind this identity?",
                },
            ],
        }
    )


def _candidate(
    document: TaskDocument, *, path: str = "master/01_leaf.json"
) -> ResolvedTaskDocument:
    return ResolvedTaskDocument(
        ref=TaskDocumentRef(repository="repo", path=path),
        path=Path("/coordination/tasks/repo") / path,
        document=document,
    )


def _digest(root: Path, document: TaskDocument) -> str:
    return task_intent_identity(root, _candidate(document)).digest


@pytest.mark.parametrize("field", ["id", "title", "outcome", "substeps"])
def test_every_step_obligation_field_changes_intent(tmp_path: Path, field: str) -> None:
    document = _document()
    step = document.steps[0]
    value: object = (
        [SubStep(id="S1.2", title="Changed child")] if field == "substeps" else "Changed"
    )
    mutated = document.model_copy(update={"steps": [step.model_copy(update={field: value})]})
    assert _digest(tmp_path, mutated) != _digest(tmp_path, document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "done"),
        (
            "disposition",
            StepDisposition(
                reason="Audit-only skip rationale",
                recordedAt="2026-09-02T00:00:00+00:00",
            ),
        ),
    ],
)
def test_every_step_progress_or_audit_field_is_excluded(
    tmp_path: Path, field: str, value: object
) -> None:
    document = _document()
    step = document.steps[0]
    mutated = document.model_copy(update={"steps": [step.model_copy(update={field: value})]})
    assert _digest(tmp_path, mutated) == _digest(tmp_path, document)


def test_task_document_ref_is_part_of_leaf_identity(tmp_path: Path) -> None:
    document = _document()
    first = task_intent_identity(tmp_path, _candidate(document))
    moved = task_intent_identity(tmp_path, _candidate(document, path="other/01_leaf.json"))
    assert first != moved


def _approved_packet(
    path: Path,
    *,
    approval: str | None = "[durable ruling](../task.md)",
    id_field: str = "Stable ID",
    stable_id: str = "R-1",
    version: str = "v1",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = [
        "# Requirement",
        "",
        "| Field | Value |",
        "| ----- | ----- |",
        f"| {id_field} | `{stable_id}` |",
        f"| Version | `{version}` |",
    ]
    if approval is not None:
        metadata.append(f"| Developer approval | {approval} |")
    path.write_text(
        "\n".join(
            [
                *metadata,
                "",
                "## Normative Requirement",
                "Exact packet text.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "stable_id,version",
    [("CCR-R02", "v2"), ("CCR-R23", "v1")],
)
def test_typed_approved_packet_refs_are_supplemental_and_version_addressed(
    tmp_path: Path,
    stable_id: str,
    version: str,
) -> None:
    relative_path = f"requirements/{stable_id}-{version}.md"
    packet = tmp_path / relative_path
    _approved_packet(packet, stable_id=stable_id, version=version)
    reference = ApprovedRequirementPacketRef(
        path=relative_path,
        stableId=stable_id,
        version=version,
    )
    document = _document().model_copy(
        update={"requirements": ["Keep current exact task text.", reference]}
    )
    projection = task_intent_projection(tmp_path, _candidate(document))
    assert [item.kind for item in projection.requirements] == [
        "exact-text",
        "approved-requirement-packet",
    ]
    assert projection.requirements[1].model_dump() == {
        "kind": "approved-requirement-packet",
        "path": relative_path,
        "stableId": stable_id,
        "version": version,
    }


@pytest.mark.parametrize(
    "approval",
    [
        None,
        "",
        "approved",
        "pending",
        "not approved",
        "rejected",
        "revoked",
        "withdrawn",
        "This packet is approved",
        "[approved](../task.md)",
        (
            "[260831-CCR autonomous execution ruling](../task.md#decisions), "
            "`2026-08-31T21:12:50+02:00`"
        ),
    ],
)
def test_packet_approval_prose_is_non_authoritative_identity_invariant(
    tmp_path: Path,
    approval: str | None,
) -> None:
    packet = tmp_path / "requirements" / "R-1-v1.md"
    reference = ApprovedRequirementPacketRef(
        path="requirements/R-1-v1.md", stableId="R-1", version="v1"
    )
    document = _document().model_copy(update={"requirements": ["Keep exact text.", reference]})
    _approved_packet(packet)
    baseline = task_intent_identity(tmp_path, _candidate(document))
    _approved_packet(packet, approval=approval)
    projection = task_intent_projection(tmp_path, _candidate(document))
    assert projection.requirements[1].kind == "approved-requirement-packet"
    assert task_intent_identity(tmp_path, _candidate(document)) == baseline


def test_approval_like_prose_cannot_create_a_typed_packet_reference(tmp_path: Path) -> None:
    _approved_packet(tmp_path / "requirements" / "R-1-v1.md")
    document = _document().model_copy(
        update={
            "requirements": ["approved; [durable ruling](../task.md) for requirements/R-1-v1.md"]
        }
    )
    projection = task_intent_projection(tmp_path, _candidate(document))
    assert [item.kind for item in projection.requirements] == ["exact-text"]


def test_duplicate_packet_metadata_refuses_as_ambiguous(tmp_path: Path) -> None:
    packet = tmp_path / "requirements" / "R-1-v1.md"
    _approved_packet(packet)
    packet.write_text(
        packet.read_text(encoding="utf-8").replace(
            "| Version | `v1` |",
            "| Version | `v1` |\n| Version | `v1` |",
        ),
        encoding="utf-8",
    )
    reference = ApprovedRequirementPacketRef(
        path="requirements/R-1-v1.md", stableId="R-1", version="v1"
    )
    document = _document().model_copy(update={"requirements": ["Exact.", reference]})
    with pytest.raises(TaskIntentError) as caught:
        task_intent_projection(tmp_path, _candidate(document))
    assert caught.value.status == "task-intent-requirement-packet-metadata-ambiguous"
