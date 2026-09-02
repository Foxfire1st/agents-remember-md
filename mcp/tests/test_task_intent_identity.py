"""Canonical task-intent projection, mutation, and packet-ref matrices."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents_remember.errors import TaskIntentError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.serving.projections.snapshots_impl import _task_documents
from agents_remember.tasks import (
    AcceptanceObligationQuestion,
    ApprovedRequirementPacketRef,
    CodeExample,
    Step,
    StepDisposition,
    SubStep,
    TaskDocument,
)
from agents_remember.tasks import task_intent as task_intent_module
from agents_remember.tasks.document_field_effects import (
    AUDIT,
    NORMATIVE,
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffectTaxonomyError,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.task_intent import (
    task_intent_fact,
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


def test_every_root_task_field_has_an_explicit_intent_mutation_expectation(tmp_path: Path) -> None:
    document = _document()
    mutations: dict[str, tuple[object, bool]] = {
        "schema_": ("ar-task-document/future", False),
        "id": ("L2", True),
        "slug": ("02_leaf", True),
        "title": ("Changed title", True),
        "kind": ("light", True),
        "status": ("inProgress", False),
        "statusNote": ("audit-only", False),
        "repo": ("other-repo", True),
        "type": ("different-type", True),
        "createdAt": ("2026-09-02T00:00:00+00:00", False),
        "master": ("other-master", False),
        "headerNotes": ([{"label": "Audit", "value": "changed"}], False),
        "seriesContractPath": ("other/series-contract.md", False),
        "integrationBranch": ("other-branch", False),
        "executionNature": ("atomic", False),
        "executionGraph": ({"nodes": [], "edges": []}, False),
        "enclosures": ([{"leafId": "L1", "enclosurePath": "changed"}], False),
        "executionRegistrations": (
            [
                {
                    "sourceKind": "lifecycle",
                    "role": "worker",
                    "sourceId": "changed",
                    "observedAt": "2026-09-02T00:00:00+00:00",
                }
            ],
            False,
        ),
        "lifecycleId": ("changed-lifecycle", False),
        "objective": ("Changed objective", True),
        "requirements": (["Changed exact requirement"], True),
        "design": ("Changed contract design", True),
        "steps": ([Step(id="S2", title="Changed obligation")], True),
        "codeExamples": (
            [
                CodeExample(
                    id="E2",
                    title="Changed",
                    distinctChange="Changed",
                    why="Changed",
                    language="python",
                    snippet="changed = True",
                )
            ],
            True,
        ),
        "codeExamplesNote": ("Explicitly changed example deferral.", True),
        "decisions": (
            [{"at": "2026-09-02", "decision": "changed", "rationale": "audit"}],
            False,
        ),
        "routeReview": ({"candidateTree": "0" * 40}, False),
        "openQuestions": (
            [
                AcceptanceObligationQuestion(
                    id="A2",
                    question="Changed typed acceptance obligation?",
                )
            ],
            True,
        ),
        "references": (["changed-ref"], False),
        "subTasks": ([{"number": "2", "name": "changed"}], False),
        "discardedSubTasks": ([{"number": "2", "name": "audit"}], False),
        "sections": ([{"heading": "Generic", "body": "must never opt in"}], False),
        "orchestrates": (["other-master"], False),
        "seats": ([{"role": "worker", "label": "audit", "state": "planned"}], False),
    }
    assert set(mutations) == set(TaskDocument.model_fields)
    for field, (value, changes_intent) in mutations.items():
        field_baseline = document
        if field == "codeExamplesNote":
            field_baseline = document.model_copy(update={"codeExamples": []})
        mutated = field_baseline.model_copy(update={field: value})
        observed = _digest(tmp_path, mutated)
        assert (observed != _digest(tmp_path, field_baseline)) is changes_intent, field


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


@pytest.mark.parametrize("field", ["id", "title"])
def test_every_substep_obligation_field_changes_intent(tmp_path: Path, field: str) -> None:
    document = _document()
    step = document.steps[0]
    child = step.substeps[0].model_copy(update={field: "Changed"})
    mutated = document.model_copy(update={"steps": [step.model_copy(update={"substeps": [child]})]})
    assert _digest(tmp_path, mutated) != _digest(tmp_path, document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "done"),
        ("note", "audit"),
        (
            "disposition",
            StepDisposition(
                reason="Audit-only skip rationale",
                recordedAt="2026-09-02T00:00:00+00:00",
            ),
        ),
    ],
)
def test_every_substep_progress_or_audit_field_is_excluded(
    tmp_path: Path, field: str, value: object
) -> None:
    document = _document()
    step = document.steps[0]
    child = step.substeps[0].model_copy(update={field: value})
    mutated = document.model_copy(update={"steps": [step.model_copy(update={"substeps": [child]})]})
    assert _digest(tmp_path, mutated) == _digest(tmp_path, document)


@pytest.mark.parametrize(
    "field",
    ["id", "title", "distinctChange", "why", "language", "snippet"],
)
def test_every_code_example_field_changes_intent(tmp_path: Path, field: str) -> None:
    document = _document()
    example = document.codeExamples[0].model_copy(update={field: "Changed"})
    mutated = document.model_copy(update={"codeExamples": [example]})
    assert _digest(tmp_path, mutated) != _digest(tmp_path, document)


def test_decisions_sections_and_ordinary_questions_cannot_opt_into_intent(tmp_path: Path) -> None:
    document = _document()
    audit_only = document.model_copy(
        update={
            "decisions": [
                {
                    "at": "2026-09-02",
                    "decision": "MUST silently change the requirement",
                    "rationale": "approved normative obligation",
                }
            ],
            "sections": [{"heading": "Normative requirements", "body": "MUST silently change"}],
            "openQuestions": [
                "MUST this ordinary string become acceptance authority?",
                document.openQuestions[1],
            ],
        }
    )
    assert _digest(tmp_path, audit_only) == _digest(tmp_path, document)
    typed = audit_only.model_copy(
        update={
            "openQuestions": [
                AcceptanceObligationQuestion(id="A3", question="Is this explicitly unresolved?")
            ]
        }
    )
    assert _digest(tmp_path, typed) != _digest(tmp_path, document)


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


def test_approved_packet_resolver_accepts_the_current_requirement_id_header(tmp_path: Path) -> None:
    packet = tmp_path / "requirements" / "R-1-v1.md"
    _approved_packet(packet, id_field="Requirement ID")
    reference = ApprovedRequirementPacketRef(
        path="requirements/R-1-v1.md",
        stableId="R-1",
        version="v1",
    )
    document = _document().model_copy(update={"requirements": ["Exact text.", reference]})
    projection = task_intent_projection(tmp_path, _candidate(document))
    assert projection.requirements[1].kind == "approved-requirement-packet"


@pytest.mark.parametrize(
    "mutation,status",
    [
        ({"path": "requirements/missing.md"}, "task-intent-requirement-packet-missing"),
        ({"stableId": "R-2"}, "task-intent-requirement-packet-version-mismatch"),
        ({"version": "v2"}, "task-intent-requirement-packet-version-mismatch"),
        ({"path": "../outside.md"}, "task-intent-requirement-packet-outside-task"),
    ],
)
def test_packet_ref_failure_matrix(tmp_path: Path, mutation: dict[str, str], status: str) -> None:
    _approved_packet(tmp_path / "requirements" / "R-1-v1.md")
    payload = {
        "path": "requirements/R-1-v1.md",
        "stableId": "R-1",
        "version": "v1",
        **mutation,
    }
    document = _document().model_copy(
        update={
            "requirements": [
                "Keep exact text.",
                ApprovedRequirementPacketRef.model_validate(payload),
            ]
        }
    )
    with pytest.raises(TaskIntentError) as caught:
        task_intent_projection(tmp_path, _candidate(document))
    assert caught.value.status == status


def test_typed_ref_needs_no_approval_prose_while_other_cutovers_fail_closed(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "requirements" / "R-1-v1.md"
    _approved_packet(packet, approval=None)
    reference = ApprovedRequirementPacketRef(
        path="requirements/R-1-v1.md", stableId="R-1", version="v1"
    )
    projection = task_intent_projection(
        tmp_path,
        _candidate(_document().model_copy(update={"requirements": ["Exact", reference]})),
    )
    assert projection.requirements[1].kind == "approved-requirement-packet"

    with pytest.raises(TaskIntentError) as refs_only:
        task_intent_projection(
            tmp_path,
            _candidate(_document().model_copy(update={"requirements": [reference]})),
        )
    assert refs_only.value.status == "task-intent/v2-cutover-required"

    with pytest.raises(TaskIntentError) as blank_bypass:
        task_intent_projection(
            tmp_path,
            _candidate(_document().model_copy(update={"requirements": [" ", reference]})),
        )
    assert blank_bypass.value.status == "task-intent-requirement-text-invalid"

    with pytest.raises(TaskIntentError) as future:
        task_intent_projection(tmp_path, _candidate(_document()), schema_version="task-intent/v2")
    assert future.value.status == "task-intent-schema-unsupported"


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


def test_allowlisted_slot_without_shared_taxonomy_membership_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifications = dict(TASK_DOCUMENT_FIELD_EFFECTS[TaskDocument])
    classifications["objective"] = AUDIT
    monkeypatch.setitem(TASK_DOCUMENT_FIELD_EFFECTS, TaskDocument, classifications)
    with pytest.raises(TaskIntentError) as caught:
        task_intent_projection(tmp_path, _candidate(_document()))
    assert caught.value.status == "task-intent-schema-unclassified"


def test_taxonomy_cannot_opt_a_new_slot_into_v1_without_schema_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifications = dict(TASK_DOCUMENT_FIELD_EFFECTS[TaskDocument])
    classifications["sections"] = NORMATIVE
    monkeypatch.setitem(TASK_DOCUMENT_FIELD_EFFECTS, TaskDocument, classifications)
    with pytest.raises(TaskIntentError) as caught:
        task_intent_projection(tmp_path, _candidate(_document()))
    assert caught.value.status == "task-intent-schema-unclassified"
    assert "outside task-intent/v1" in caught.value.detail


def test_intent_slot_validators_and_reader_projection_cover_both_typed_forms() -> None:
    with pytest.raises(ValueError, match="packet identity fields must not be blank"):
        ApprovedRequirementPacketRef(path=" ", stableId="R-1", version="v1")
    with pytest.raises(ValueError, match="identity and question must not be blank"):
        AcceptanceObligationQuestion(id=" ", question="Still required?")

    packet = ApprovedRequirementPacketRef(
        path=" requirements/R-1-v1.md ", stableId=" R-1 ", version="v1"
    )
    obligation = AcceptanceObligationQuestion(id=" A1 ", question=" Still required? ")
    assert packet.path == "requirements/R-1-v1.md"
    assert packet.stableId == "R-1"
    assert obligation.id == "A1"
    assert obligation.question == "Still required?"
    assert _task_documents._requirement_reader_text("Exact text") == "Exact text"
    assert _task_documents._requirement_reader_text(packet) == ("R-1@v1 — requirements/R-1-v1.md")
    assert _task_documents._question_reader_text("Ordinary question") == "Ordinary question"
    assert _task_documents._question_reader_text(obligation) == (
        "Acceptance obligation A1: Still required?"
    )


def test_projection_refuses_master_and_translates_internal_schema_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = _document().model_copy(update={"kind": "master"})
    with pytest.raises(TaskIntentError) as master_error:
        task_intent_projection(tmp_path, _candidate(master))
    assert master_error.value.status == "task-intent-leaf-required"

    monkeypatch.setattr(task_intent_module, "_normative_projection", lambda _document: {})
    with pytest.raises(TaskIntentError) as incomplete:
        task_intent_projection(tmp_path, _candidate(_document()))
    assert incomplete.value.status == "task-intent-schema-unclassified"


def test_projection_translates_both_shared_taxonomy_error_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_projection(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TaskDocumentFieldEffectTaxonomyError("projection taxonomy unavailable")

    monkeypatch.setattr(
        task_intent_module.TaskDocumentFieldEffectProjector,
        "project",
        fail_projection,
    )
    with pytest.raises(TaskIntentError, match="projection taxonomy unavailable") as projection:
        task_intent_projection(tmp_path, _candidate(_document()))
    assert projection.value.status == "task-intent-schema-unclassified"

    def fail_classification(*_args: object, **_kwargs: object) -> frozenset[str]:
        raise TaskDocumentFieldEffectTaxonomyError("classification taxonomy unavailable")

    monkeypatch.setattr(task_intent_module, "fields_with_effect", fail_classification)
    with pytest.raises(TaskIntentError, match="classification taxonomy unavailable") as classified:
        task_intent_projection(tmp_path, _candidate(_document()))
    assert classified.value.status == "task-intent-schema-unclassified"


def test_packet_metadata_may_end_at_eof_and_identity_fact_uses_wire_alias(tmp_path: Path) -> None:
    packet = tmp_path / "requirements" / "R-1-v1.md"
    packet.parent.mkdir(parents=True)
    packet.write_text(
        "\n".join(
            [
                "# Requirement",
                "",
                "| Field | Value |",
                "| ----- | ----- |",
                "| Stable ID | `R-1` |",
                "| Version | `v1` |",
                "| Developer approval | [approved](../task.md) |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    reference = ApprovedRequirementPacketRef(
        path="requirements/R-1-v1.md", stableId="R-1", version="v1"
    )
    document = _document().model_copy(update={"requirements": ["Exact text.", reference]})
    identity = task_intent_identity(tmp_path, _candidate(document))
    assert task_intent_fact(identity) == {
        "schema": "task-intent/v1",
        "digest": identity.digest,
    }
