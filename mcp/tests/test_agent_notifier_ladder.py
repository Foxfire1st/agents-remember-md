from __future__ import annotations

from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology

SPRINT_REF = TaskDocumentRef(repository="repo-a", path="sprint/task.json")
MASTER_REF = TaskDocumentRef(repository="repo-a", path="260707_master/task.json")
LEAF_1_REF = TaskDocumentRef(repository="repo-a", path="260707_master/leaf-1.json")


def _task_doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": values.pop("id"),
            "slug": values.pop("slug"),
            "title": values.pop("title"),
            "kind": values.pop("kind"),
            "repo": "repo-a",
            "createdAt": "2026-07-07T00:00",
            **values,
        }
    )


def _write_topology(root: Path) -> TaskDocumentTopology:
    """Create one sprint/master and enough real leaf documents for every ladder fixture."""
    leaf_names = tuple(f"leaf-{index}" for index in range(60))
    task_root = root / "tasks" / "repo-a"
    write_task_doc(
        task_root / "sprint",
        _task_doc(
            id="SPRINT",
            slug="sprint",
            title="Sprint",
            kind="master",
            orchestrates=["260707_master"],
        ),
    )
    write_task_doc(
        task_root / "260707_master",
        _task_doc(
            id="MASTER",
            slug="260707_master",
            title="Master",
            kind="master",
            subTasks=[
                {
                    "number": leaf,
                    "name": leaf,
                    "file": f"{leaf}.md",
                    "status": "inProgress",
                }
                for leaf in leaf_names
            ],
        ),
    )
    for leaf in leaf_names:
        write_task_doc(
            task_root / "260707_master",
            _task_doc(
                id=leaf,
                slug=leaf,
                title=leaf,
                kind="subTask",
                master="task.md",
            ),
        )
    return TaskDocumentTopology(root)


def _leaf_ref(index: int) -> TaskDocumentRef:
    return TaskDocumentRef(repository="repo-a", path=f"260707_master/leaf-{index}.json")
