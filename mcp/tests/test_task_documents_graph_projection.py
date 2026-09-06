"""Task-documents projection wiring for the render-ready graph view (L12-R4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents_remember.serving.projections.snapshots_impl._task_documents import (
    read_task_documents,
)
from agents_remember.tasks import TaskDocument, write_task_doc
from test_observer_projection import FRESH

REPO = "repo-a"


def _doc(**over: object) -> TaskDocument:
    base: dict[str, object] = {
        "id": "D",
        "slug": "task",
        "title": "Demo",
        "kind": "light",
        "repo": REPO,
        "createdAt": "2026-01-01T00:00",
    }
    base.update(over)
    return TaskDocument.model_validate(base)


class TaskDocumentsGraphViewProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.coord = Path(self._dir.name)

    def _master(self, name: str, *, status: str, nature: str, rows: list[dict[str, str]]) -> None:
        write_task_doc(
            self.coord / "tasks" / REPO / name,
            TaskDocument.model_validate(
                {
                    "id": name.upper(),
                    "slug": name,
                    "title": f"Title {name}",
                    "kind": "master",
                    "status": status,
                    "repo": REPO,
                    "createdAt": "2026-08-15T00:00:00+00:00",
                    "executionNature": nature,
                    "subTasks": [
                        {
                            "number": row["number"],
                            "name": row["name"],
                            "status": row["status"],
                        }
                        for row in rows
                    ],
                }
            ),
        )

    def test_segmented_master_scenario_projects_titles_and_predecessors(self) -> None:
        write_task_doc(
            self.coord / "tasks" / REPO / "sprint",
            _doc(
                id="SPRINT",
                kind="master",
                title="Sprint",
                orchestrates=["master-a", "atomic-f"],
                executionGraph={
                    "nodes": [
                        {
                            "kind": "segment",
                            "ref": {"repository": REPO, "path": "master-a/task.json"},
                            "leafIds": ["A-L1", "A-L2"],
                        },
                        {"ref": {"repository": REPO, "path": "atomic-f/task.json"}},
                        {
                            "kind": "segment",
                            "ref": {"repository": REPO, "path": "master-a/task.json"},
                            "leafIds": ["A-L3"],
                        },
                    ],
                    "edges": [
                        {
                            "predecessor": {
                                "ref": {"repository": REPO, "path": "master-a/task.json"},
                                "leafId": "A-L1",
                            },
                            "successor": {
                                "ref": {"repository": REPO, "path": "atomic-f/task.json"}
                            },
                            "reason": "early segment gates the atomic block",
                        },
                        {
                            "predecessor": {
                                "ref": {"repository": REPO, "path": "atomic-f/task.json"}
                            },
                            "successor": {
                                "ref": {"repository": REPO, "path": "master-a/task.json"},
                                "leafId": "A-L3",
                            },
                            "reason": "the atomic block gates the late segment",
                        },
                    ],
                },
            ),
        )
        self._master(
            "master-a",
            status="inProgress",
            nature="organizational",
            rows=[
                {"number": "A-L1", "name": "Leaf one", "status": "Completed"},
                {"number": "A-L2", "name": "Leaf two", "status": "inProgress"},
                {"number": "A-L3", "name": "Leaf three", "status": "planning"},
            ],
        )
        self._master(
            "atomic-f",
            status="planning",
            nature="atomic",
            rows=[{"number": "F-L1", "name": "F leaf", "status": "planning"}],
        )

        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)
        sprint = next(node for node in nodes if node.id == "SPRINT")
        view = sprint.executionGraphView
        self.assertIsNotNone(view)
        assert view is not None
        self.assertEqual([node.waveIndex for node in view.nodes], [1, 2, 3])
        early, atomic, late = view.nodes
        self.assertEqual(early.leafTitles, ["Leaf one", "Leaf two"])
        self.assertEqual(atomic.frontierState, "waiting")
        self.assertEqual(
            [(p.predecessorTitle, p.reason) for p in atomic.predecessors],
            [("Title master-a", "early segment gates the atomic block")],
        )
        # The late segment is waiting: its atomic predecessor is not landed (planning).
        self.assertEqual(late.frontierState, "waiting")
        self.assertEqual(
            [(p.predecessorTitle, p.reason) for p in late.predecessors],
            [("Title atomic-f", "the atomic block gates the late segment")],
        )

    def test_duplicate_local_leaf_numbers_keep_master_qualified_titles(self) -> None:
        write_task_doc(
            self.coord / "tasks" / REPO / "sprint",
            _doc(
                id="SPRINT",
                kind="master",
                title="Sprint",
                orchestrates=["master-a", "master-b"],
                executionGraph={
                    "nodes": [
                        {
                            "kind": "segment",
                            "ref": {"repository": REPO, "path": "master-a/task.json"},
                            "leafIds": ["L1"],
                        },
                        {"ref": {"repository": REPO, "path": "master-b/task.json"}},
                    ],
                    "edges": [
                        {
                            "predecessor": {
                                "ref": {"repository": REPO, "path": "master-a/task.json"},
                                "leafId": "L1",
                            },
                            "successor": {
                                "repository": REPO,
                                "path": "master-b/task.json",
                            },
                            "reason": "A leaf gates B",
                        }
                    ],
                },
            ),
        )
        self._master(
            "master-a",
            status="inProgress",
            nature="organizational",
            rows=[{"number": "L1", "name": "Title from A", "status": "inProgress"}],
        )
        self._master(
            "master-b",
            status="planning",
            nature="atomic",
            rows=[{"number": "L1", "name": "Title from B", "status": "planning"}],
        )

        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)
        sprint = next(node for node in nodes if node.id == "SPRINT")
        view = sprint.executionGraphView
        assert view is not None
        segment = next(node for node in view.nodes if node.kind == "segment")
        self.assertEqual(segment.leafTitles, ["Title from A"])
