"""Sprint/master/leaf structural seat hierarchy regression coverage (EFA-L19)."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agents_remember.controlplane.signal_routing import RoutedOwner, derive_architect_owner
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import TerminalCatalogEntry
from agents_remember.serving.structural_seats import StructuralSeatError, StructuralSeatResolver
from agents_remember.serving.terminal_catalog import TerminalCatalog
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology

STAMP = "2026-08-10T00:00:00+00:00"


def _ref(repo: str, path: str) -> TaskDocumentRef:
    return TaskDocumentRef(repository=repo, path=path)


SPRINT_A = _ref("repo-a", "sprint-a/task.json")
MASTER_A = _ref("repo-a", "master-a/task.json")
LEAF_A = _ref("repo-a", "master-a/leaf-a.json")
SPRINT_B = _ref("repo-b", "sprint-b/task.json")
MASTER_B = _ref("repo-b", "master-b/task.json")
LEAF_B = _ref("repo-b", "master-b/leaf-b.json")


def _task_doc(repo: str, **values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": values.pop("id"),
            "slug": values.pop("slug"),
            "title": values.pop("title"),
            "kind": values.pop("kind"),
            "repo": repo,
            "createdAt": "2026-08-10T00:00",
            **values,
        }
    )


def _write_repo_topology(root: Path, *, repo: str, sprint: str, master: str, leaf: str) -> None:
    task_root = root / "tasks" / repo
    write_task_doc(
        task_root / sprint,
        _task_doc(
            repo,
            id=sprint,
            slug=sprint,
            title=sprint,
            kind="master",
            orchestrates=[master],
        ),
    )
    write_task_doc(
        task_root / master,
        _task_doc(
            repo,
            id=master,
            slug=master,
            title=master,
            kind="master",
            subTasks=[
                {
                    "number": leaf,
                    "name": leaf,
                    "file": f"{leaf}.md",
                    "status": "inProgress",
                }
            ],
        ),
    )
    write_task_doc(
        task_root / master,
        _task_doc(
            repo,
            id=leaf,
            slug=leaf,
            title=leaf,
            kind="subTask",
            master="task.md",
        ),
    )


def _seat(
    session_id: str,
    document: TaskDocumentRef,
    role: str,
    *,
    status: str = "running",
    replacement_for: TaskDocumentRef | None = None,
) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=session_id,
        kind="harness",
        harness="claude",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("claude",),
        created_at=STAMP,
        last_attached_at=STAMP,
        status=status,  # type: ignore[arg-type]
        task_document_ref=document,
        seat_role=role,
        spawn_role=role,
        replacement_for_task_document_ref=replacement_for,
    )


class StructuralRoleSeatTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        _write_repo_topology(
            self.root,
            repo="repo-a",
            sprint="sprint-a",
            master="master-a",
            leaf="leaf-a",
        )
        _write_repo_topology(
            self.root,
            repo="repo-b",
            sprint="sprint-b",
            master="master-b",
            leaf="leaf-b",
        )
        self.catalog = TerminalCatalog(self.root / "terminal-sessions.json")
        self.topology = TaskDocumentTopology(self.root)
        self.resolver = StructuralSeatResolver(self.catalog, self.topology)

    def test_same_role_on_different_sprints_never_crosses_repository_scope(self) -> None:
        self.catalog.upsert(_seat("architect-a", SPRINT_A, "architect"))
        self.catalog.upsert(_seat("architect-b", SPRINT_B, "architect"))

        self.assertEqual(
            derive_architect_owner(self.catalog, self.topology, task_document_ref=LEAF_A),
            RoutedOwner(
                role="architect",
                task_document_ref=SPRINT_A,
                agent_id="architect-a",
            ),
        )
        self.assertEqual(
            derive_architect_owner(self.catalog, self.topology, task_document_ref=LEAF_B),
            RoutedOwner(
                role="architect",
                task_document_ref=SPRINT_B,
                agent_id="architect-b",
            ),
        )

    def test_duplicate_current_occupants_fail_closed(self) -> None:
        self.catalog.upsert(_seat("one", SPRINT_A, "orchestrator"))
        self.catalog.upsert(_seat("two", SPRINT_A, "orchestrator"))

        with self.assertRaisesRegex(StructuralSeatError, "multiple running occupants"):
            self.resolver.current(SPRINT_A, "orchestrator")

    def test_role_altitude_mismatch_fails_before_any_occupant_lookup(self) -> None:
        with self.assertRaisesRegex(StructuralSeatError, "requires a sprint document"):
            self.resolver.current(LEAF_A, "architect")

    def test_reviewer_parent_is_exact_for_each_review_seam(self) -> None:
        cases = (
            (LEAF_A, MASTER_A, "manager"),
            (MASTER_A, MASTER_A, "manager"),
            (SPRINT_A, SPRINT_A, "architect"),
            (SPRINT_A, SPRINT_A, "orchestrator"),
        )
        for index, (document, parent_document, parent_role) in enumerate(cases):
            with self.subTest(document=document.key, parent_role=parent_role):
                reviewer = replace(
                    _seat(f"reviewer-{index}", document, "reviewer"),
                    structural_parent_task_document_ref=parent_document,
                    structural_parent_role=parent_role,
                )
                self.assertEqual(
                    self.resolver.parent_address(reviewer),
                    (parent_document, parent_role),
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
