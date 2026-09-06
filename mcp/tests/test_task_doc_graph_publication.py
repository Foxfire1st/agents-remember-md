"""Focused task-publication graph-cardinality and title-context tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agents_remember.application.task_docs import task_doc_publication
from agents_remember.application.task_docs.task_doc_graph_titles import (
    build_publication_batch_graph_titles,
    require_single_graph_document,
)
from agents_remember.application.task_docs.task_doc_publication import (
    TaskDocPublication,
    publish_task_doc_set,
)
from agents_remember.application.task_docs.task_doc_route_review import TaskDocError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import TaskDocument

REPO = "repo-a"


def _graph_document(name: str, title: str) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": name.upper(),
            "slug": "task",
            "kind": "master",
            "title": title,
            "repo": REPO,
            "createdAt": "2026-08-24T00:00:00+00:00",
            "orchestrates": [name, f"gate-{name}"],
            "subTasks": [
                {
                    "number": "L1",
                    "name": f"{title} leaf",
                    "status": "planning",
                }
            ],
            "executionGraph": {
                "nodes": [
                    {
                        "kind": "segment",
                        "ref": {
                            "repository": REPO,
                            "path": f"{name}/task.json",
                        },
                        "leafIds": ["L1"],
                    },
                    {
                        "repository": REPO,
                        "path": f"gate-{name}/task.json",
                    },
                ],
                "edges": [
                    {
                        "predecessor": {
                            "ref": {
                                "repository": REPO,
                                "path": f"{name}/task.json",
                            },
                            "leafId": "L1",
                        },
                        "successor": {
                            "repository": REPO,
                            "path": f"gate-{name}/task.json",
                        },
                        "reason": "the first master gates the second",
                    }
                ],
            },
        }
    )


def _plain_document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "PLAIN",
            "slug": "task",
            "kind": "master",
            "title": "Plain",
            "repo": REPO,
            "createdAt": "2026-08-24T00:00:00+00:00",
        }
    )


class TaskDocGraphPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = _graph_document("first", "First graph")
        self.second = _graph_document("second", "Second graph")

    def test_plain_or_single_graph_document_is_the_supported_batch_shape(self) -> None:
        self.assertIsNone(require_single_graph_document([_plain_document()]))
        self.assertIs(
            require_single_graph_document([_plain_document(), self.first]),
            self.first,
        )
        first_ref = TaskDocumentRef(repository=REPO, path="first/task.json")
        titles = build_publication_batch_graph_titles([(first_ref, Path("first"), self.first)])
        assert titles is not None
        self.assertEqual(titles.leaf_titles, {(first_ref, "L1"): "First graph leaf"})

    def test_two_graph_documents_refuse_before_task_or_projection_publication(self) -> None:
        task_root = self.root / "tasks" / REPO / "candidate"
        task_root.mkdir(parents=True)
        targets = {
            task_root / "task.json": b"sentinel task bytes\n",
            task_root / "task.md": b"sentinel markdown bytes\n",
        }
        for path, content in targets.items():
            path.write_bytes(content)
        publisher = mock.Mock(return_value=[])
        context = TaskDocPublication(
            config=SimpleNamespace(coordination_root=self.root),  # type: ignore[arg-type]
            target_repo_id=REPO,
            task_root=task_root,
            original=None,
            candidate=self.first,
            documents=[self.first, self.second],
            source_snapshots=(),
            publisher=publisher,
        )
        with (
            mock.patch.object(task_doc_publication, "publish_task_fact_mutation") as mutation,
            self.assertRaises(TaskDocError) as raised,
        ):
            publish_task_doc_set(context)
        self.assertIn("graph-cardinality", str(raised.exception))
        publisher.assert_not_called()
        mutation.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in targets}, targets)
