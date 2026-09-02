"""Public lifecycle-control helpers for tests that own a current generation."""

from datetime import UTC, datetime
from pathlib import Path

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationKind,
    LifecycleOperationProjection,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleControlAction
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import Section, TaskDocument, read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.store import json_path_for
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    LifecycleControlCommand,
    control_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    require_matching_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.queue.closeout_queue_evidence import (
    JUDGMENT_REGISTER_HEADER,
    JUDGMENT_REGISTER_HEADING,
    PRIORITY_REGISTER_HEADER,
    PRIORITY_REGISTER_HEADING,
)
from agents_remember.worktrees.route_review import build_route_review, document_ref
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract

FIXTURE_GRADE_JUDGMENT = "TEST-FIXTURE-BELOW-SCHEDULING"


def publish_completed_disposition_task_authority(
    contract: WorktreeContract,
    *,
    sprint_owned: bool,
) -> DeclaredCaller:
    """Publish real task, review, and planning authority for disposition tests."""

    leaf_slug = contract.leaf_id.lower()
    review_ref = "notes/reports/lifecycle-disposition-review.md"
    review = contract.task_root / review_ref
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("# Lifecycle disposition review\n\nPass.\n", encoding="utf-8")
    leaf_path = contract.task_root / f"{leaf_slug}.json"
    if leaf_path.is_file():
        leaf = TaskDocument.model_validate(
            {
                **read_task_doc(leaf_path).model_dump(mode="json"),
                "status": "Completed",
                "enclosures": [
                    {
                        "leafId": contract.leaf_id,
                        "enclosurePath": contract.contract_path.as_posix(),
                    }
                ],
            }
        )
    else:
        leaf = TaskDocument.model_validate(
            {
                "id": contract.leaf_id,
                "slug": leaf_slug,
                "title": "Lifecycle disposition leaf",
                "kind": "subTask",
                "status": "Completed",
                "repo": contract.repo_name,
                "createdAt": "2026-08-22T00:00:00+00:00",
                "enclosures": [
                    {
                        "leafId": contract.leaf_id,
                        "enclosurePath": contract.contract_path.as_posix(),
                    }
                ],
                "steps": [{"id": "S1", "title": "Ready", "status": "done"}],
            }
        )
        leaf_path = json_path_for(contract.task_root, leaf)
    candidate_ref = document_ref(contract, leaf_path)
    master_ref = document_ref(contract, contract.task_root / "task.json")
    review_record = build_route_review(
        contract,
        ResolvedTaskDocument(ref=candidate_ref, path=leaf_path, document=leaf),
        {
            "verdict": "pass",
            "verdictRef": review_ref,
            "routes": [
                {
                    "route": "lifecycle-disposition",
                    "verdict": "pass",
                    "evidenceRef": review_ref,
                }
            ],
        },
        now=datetime(2026, 8, 22, tzinfo=UTC),
    )
    write_task_doc(
        contract.task_root,
        leaf.model_copy(update={"routeReview": review_record}),
    )
    write_task_doc(
        contract.task_root,
        TaskDocument.model_validate(
            {
                "id": "DURABLE-LIFECYCLE",
                "slug": "durable-lifecycle",
                "title": "Durable lifecycle",
                "kind": "master",
                "status": "inProgress",
                "repo": contract.repo_name,
                "createdAt": "2026-08-22T00:00:00+00:00",
                "executionNature": "organizational" if sprint_owned else "atomic",
                "subTasks": [
                    {
                        "number": contract.leaf_id,
                        "name": "Lifecycle disposition leaf",
                        "file": f"{leaf_slug}.md",
                        "status": "Completed",
                    }
                ],
            }
        ),
    )
    if not sprint_owned:
        return DeclaredCaller(role="architect", task_document_ref=master_ref)

    sprint_ref = TaskDocumentRef(
        repository=contract.repo_name,
        path="lifecycle-fixture-sprint/task.json",
    )
    planning_ref = "planning.md"
    sprint_root = contract.task_root.parent / "lifecycle-fixture-sprint"
    sprint_root.mkdir(parents=True, exist_ok=True)
    (sprint_root / planning_ref).write_text(
        "# Lifecycle fixture planning evidence\n",
        encoding="utf-8",
    )
    subject = candidate_ref.key
    write_task_doc(
        sprint_root,
        TaskDocument.model_validate(
            {
                "id": "LIFECYCLE-FIXTURE-SPRINT",
                "slug": "lifecycle-fixture-sprint",
                "title": "Lifecycle fixture sprint",
                "kind": "master",
                "status": "inProgress",
                "repo": contract.repo_name,
                "createdAt": "2026-08-22T00:00:00+00:00",
                "orchestrates": [contract.task_name],
                "integrationBranch": contract.code_source_branch,
                "executionGraph": {
                    "nodes": [master_ref.model_dump(mode="json")],
                    "edges": [],
                },
                "sections": [
                    Section(
                        kind="freeform",
                        heading=JUDGMENT_REGISTER_HEADING,
                        body=_register_body(
                            JUDGMENT_REGISTER_HEADER,
                            (
                                FIXTURE_GRADE_JUDGMENT,
                                "priority",
                                subject,
                                "priority=normal",
                                "Fixture scheduling authority.",
                                planning_ref,
                                "strategist",
                                "high",
                                "",
                            ),
                        ),
                    ),
                    Section(
                        kind="freeform",
                        heading=PRIORITY_REGISTER_HEADING,
                        body=_register_body(
                            PRIORITY_REGISTER_HEADER,
                            (subject, "normal", "", FIXTURE_GRADE_JUDGMENT),
                        ),
                    ),
                ],
            }
        ),
    )
    return DeclaredCaller(role="orchestrator", task_document_ref=sprint_ref)


def _register_body(header: tuple[str, ...], row: tuple[str, ...]) -> str:
    return "\n".join(
        (
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            "| " + " | ".join(row) + " |",
        )
    )


def control_current_generation(
    contract_path: Path,
    kind: LifecycleOperationKind,
    action: LifecycleControlAction,
    *,
    intent_note: str = "exercise exact lifecycle generation",
) -> LifecycleOperationProjection:
    contract = load_contract(contract_path)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, kind))
    record = store.read()
    assert record is not None
    return control_operation(
        LifecycleControlCommand(
            admitted_contract=contract,
            admitted_location=require_matching_lifecycle_operation_location(contract),
            configured_authority=record.input.configPath,
            kind=kind,
            action=action,
            expected_generation=record.generation,
            intent_note=intent_note,
        )
    )


def cancel_current_generation(
    contract_path: Path,
    kind: LifecycleOperationKind,
) -> LifecycleOperationProjection:
    return control_current_generation(contract_path, kind, "cancel")
