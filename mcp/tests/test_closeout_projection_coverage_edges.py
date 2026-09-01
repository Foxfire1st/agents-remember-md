"""Typed error-boundary coverage for closeout projection adapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agents_remember.tasks import write_task_doc
from agents_remember.tasks.document import SprintExecutionGraph, SubTaskRef, TaskDocument
from agents_remember.tasks.document_field_effects import TaskDocumentFieldEffectTaxonomyError
from agents_remember.tasks.semantic_topology_graph import SemanticTopologyGraphIndexError
from agents_remember.worktrees import task_leaf_binding as task_binding_module
from agents_remember.worktrees.queue import closeout_projection as projection_module
from agents_remember.worktrees.queue import closeout_projection_source_facts as source_module
from agents_remember.worktrees.queue import closeout_queue_graph as queue_graph_module
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from agents_remember.worktrees.task_leaf_binding import TaskLeafBindingError
from test_closeout_projection_member_helpers import MASTER, SPRINT, _documents

NOW = "2026-08-31T00:00:00+00:00"
REPOSITORY = "agents-remember"


def _document(
    *,
    kind: str,
    identifier: str,
    slug: str,
    rows: list[SubTaskRef] | None = None,
) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": identifier,
            "slug": slug,
            "title": slug,
            "kind": kind,
            "status": "inProgress",
            "repo": REPOSITORY,
            "createdAt": NOW,
            "subTasks": [row.model_dump(mode="json") for row in rows or []],
        }
    )


class CloseoutProjectionCoverageEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sprint, self.master, self.candidate = _documents()
        self.graph = cast(SprintExecutionGraph, self.sprint.document.executionGraph)

    def test_task_source_error_retains_address_and_projects_as_unreadable(self) -> None:
        with (
            mock.patch.object(
                source_module,
                "project_model_field_effect",
                side_effect=TaskDocumentFieldEffectTaxonomyError("missing effect"),
            ),
            self.assertRaises(source_module.TaskSourceProjectionError) as raised,
        ):
            source_module.task_source_fact(self.candidate)
        self.assertEqual(raised.exception.address, self.candidate.ref.key)
        self.assertEqual(raised.exception.detail, "missing effect")

        with mock.patch.object(
            projection_module,
            "_task_census",
            side_effect=source_module.TaskSourceProjectionError(SPRINT.key, "missing effect"),
        ):
            snapshot = projection_module.capture_projection_source(
                Path("/unused"),
                SPRINT,
                timestamp=NOW,
            )
        self.assertIsNone(snapshot.identity.fingerprint)
        self.assertEqual(
            snapshot.identity.problems[0].model_dump(mode="json"),
            {
                "kind": "task",
                "address": SPRINT.key,
                "state": "unreadable",
                "errorType": "task-document-field-effect-unclassified",
                "repairAction": "classify every task-document field before rebuilding",
            },
        )

    def test_planning_error_projects_as_one_typed_source_problem(self) -> None:
        tasks = projection_module._TaskCensus(
            cast(Any, object()),
            self.sprint,
            "dag",
            (self.master,),
            (),
            (),
        )
        doors = projection_module._DoorCensus((), (), {}, ())
        with (
            mock.patch.object(projection_module, "_task_census", return_value=tasks),
            mock.patch.object(projection_module, "_door_census", return_value=doors),
            mock.patch.object(
                projection_module,
                "graph_context",
                side_effect=CloseoutQueueError("planning-invalid", "bad register"),
            ),
        ):
            snapshot = projection_module.capture_projection_source(
                Path("/unused"),
                SPRINT,
                timestamp=NOW,
            )
        self.assertEqual(
            snapshot.identity.problems[0].model_dump(mode="json"),
            {
                "kind": "task",
                "address": SPRINT.key,
                "state": "invalid",
                "errorType": "planning-invalid",
                "repairAction": "repair the canonical planning registers before rebuilding",
            },
        )

    def test_queue_graph_translates_semantic_index_refusal(self) -> None:
        with (
            mock.patch.object(
                queue_graph_module,
                "_validated_graph_documents",
                return_value=(self.sprint, self.graph, {MASTER: self.master}),
            ),
            mock.patch.object(
                queue_graph_module,
                "build_semantic_topology_graph_index",
                side_effect=SemanticTopologyGraphIndexError(
                    "semantic-index-invalid",
                    "invalid graph index",
                ),
            ),
            self.assertRaises(CloseoutQueueError) as raised,
        ):
            queue_graph_module.graph_context(
                cast(Any, object()),
                SPRINT,
                authored_graph=self.graph,
            )
        self.assertEqual(raised.exception.status, "semantic-index-invalid")
        self.assertEqual(raised.exception.detail, "invalid graph index")

    def test_leaf_binding_translates_domain_error_and_refuses_escaped_source(self) -> None:
        row = SubTaskRef(
            number="L01",
            name="Leaf",
            file="leaf.md",
            status="inProgress",
        )
        parent = _document(
            kind="master",
            identifier="MASTER",
            slug="master",
            rows=[row],
        )
        leaf = _document(kind="subTask", identifier="L01", slug="leaf")
        with tempfile.TemporaryDirectory() as temporary:
            coordination_root = Path(temporary)
            task_root = coordination_root / "tasks" / REPOSITORY / "master"
            write_task_doc(task_root, parent)
            missing_leaf = task_binding_module.resolve_leaf_task_binding(
                coordination_root,
                REPOSITORY,
                task_root,
                "L01",
            )
            self.assertIsNone(missing_leaf.leaf)
            write_task_doc(task_root, leaf)
            with (
                mock.patch.object(
                    task_binding_module,
                    "require_canonical_leaf_binding",
                    side_effect=task_binding_module.CanonicalLeafBindingError(
                        "task-leaf-binding-future",
                        "future binding failure",
                    ),
                ),
                self.assertRaises(TaskLeafBindingError) as raised,
            ):
                task_binding_module.resolve_leaf_task_binding(
                    coordination_root,
                    REPOSITORY,
                    task_root,
                    "L01",
                )
            self.assertEqual(raised.exception.status, "task-leaf-binding-future")
            self.assertEqual(raised.exception.detail, "future binding failure")

            with self.assertRaises(TaskLeafBindingError) as raised:
                task_binding_module._task_ref_for_path(
                    coordination_root,
                    REPOSITORY,
                    coordination_root / "outside" / "task.json",
                )
            self.assertEqual(raised.exception.status, "task-leaf-binding-invalid")
            self.assertEqual(
                raised.exception.detail,
                "canonical task source escapes the repository task root",
            )
