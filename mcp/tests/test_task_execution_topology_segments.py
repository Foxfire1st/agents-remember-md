"""L11 segment-graph schema, endpoint grammar, derived placement, and partition facts.

Split from ``test_task_execution_topology.py`` (file-size limit); fixtures and shared
helpers are imported from it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SubTaskRef,
    derived_leaf_placement,
    leaf_placement_facts,
    write_task_doc,
)
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology
from pydantic import ValidationError
from test_task_execution_topology import (
    MASTER_A,
    MASTER_B,
    REPOSITORY,
    SPRINT,
    _master,
)


def _segment(ref: TaskDocumentRef, leaf_ids: list[str]) -> dict[str, Any]:
    return {"kind": "segment", "ref": ref.model_dump(), "leafIds": leaf_ids}


class ExecutionGraphSegmentSchemaTests(unittest.TestCase):
    """L11-R1/R3/R4/R7: node kinds, leaf-level uniqueness, endpoint grammar, compat."""

    def test_leaf_ids_are_unique_sprint_wide(self) -> None:
        with self.assertRaisesRegex(ValidationError, "more than one node"):
            SprintExecutionGraph.model_validate(
                {
                    "nodes": [
                        _segment(MASTER_A, ["L1"]),
                        _segment(MASTER_B, ["L1"]),
                    ]
                }
            )

    def test_lump_and_segment_appearances_of_one_master_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValidationError, "mutually exclusive"):
            SprintExecutionGraph.model_validate(
                {"nodes": [MASTER_A.model_dump(), _segment(MASTER_A, ["L1"])]}
            )

    def test_edge_endpoints_address_segments_by_leaf_sample(self) -> None:
        graph = SprintExecutionGraph.model_validate(
            {
                "nodes": [
                    _segment(MASTER_A, ["L1"]),
                    _segment(MASTER_A, ["L2", "L3"]),
                    MASTER_B.model_dump(),
                ],
                "edges": [
                    {
                        "predecessor": MASTER_B.model_dump(),
                        "successor": {"ref": MASTER_A.model_dump(), "leafId": "L2"},
                        "reason": "framework first",
                        "judgmentId": "J-1",
                    }
                ],
            }
        )
        edge = graph.edges[0]
        self.assertEqual(edge.judgmentId, "J-1")
        self.assertEqual(graph.resolve_endpoint(edge.successor).leafIds, ["L2", "L3"])
        waves = graph.derived_waves()
        self.assertEqual(
            [[(node.kind, node.leafIds) for node in wave] for wave in waves],
            [
                [("segment", ["L1"]), ("master", [])],
                [("segment", ["L2", "L3"])],
            ],
        )

    def test_cycle_through_segments_is_refused(self) -> None:
        with self.assertRaisesRegex(ValidationError, "acyclic"):
            SprintExecutionGraph.model_validate(
                {
                    "nodes": [_segment(MASTER_A, ["L1"]), _segment(MASTER_B, ["L2"])],
                    "edges": [
                        {
                            "predecessor": {"ref": MASTER_A.model_dump(), "leafId": "L1"},
                            "successor": {"ref": MASTER_B.model_dump(), "leafId": "L2"},
                            "reason": "a before b",
                        },
                        {
                            "predecessor": {"ref": MASTER_B.model_dump(), "leafId": "L2"},
                            "successor": {"ref": MASTER_A.model_dump(), "leafId": "L1"},
                            "reason": "b before a",
                        },
                    ],
                }
            )


class DerivedLeafPlacementTests(unittest.TestCase):
    """L11-R2/R8: unplaced-leaf derived placement and numbering-drift hints."""

    def _graph(self, *, edge_to: str | None = None) -> SprintExecutionGraph:
        nodes: list[dict[str, Any]] = [
            _segment(MASTER_A, ["L1"]),
            _segment(MASTER_A, ["L2"]),
            MASTER_B.model_dump(),
        ]
        edges = []
        if edge_to is not None:
            edges.append(
                {
                    "predecessor": MASTER_B.model_dump(),
                    "successor": {"ref": MASTER_A.model_dump(), "leafId": edge_to},
                    "reason": "B first",
                }
            )
        return SprintExecutionGraph.model_validate({"nodes": nodes, "edges": edges})

    def test_unplaced_leaf_derives_to_the_latest_unblocked_segment(self) -> None:
        # L2's segment sits in the later wave and is blocked by incomplete MASTER-B;
        # the derived target is the latest *unblocked* segment.
        placement = derived_leaf_placement(
            self._graph(edge_to="L2"), MASTER_A, ["L1", "L2", "L3"], set()
        )
        self.assertEqual(placement.unplaced_leaf_ids, ("L3",))
        self.assertEqual(placement.derived["L3"].leafIds, ["L1"])
        self.assertFalse(placement.derived_all_blocked)
        facts = leaf_placement_facts(MASTER_A.key, placement)
        self.assertEqual(
            facts,
            [
                {
                    "kind": "unplaced-leaf",
                    "master": MASTER_A.key,
                    "leafId": "L3",
                    "derivedSegmentLeafs": ["L1"],
                    "derivedAllSegmentsBlocked": False,
                }
            ],
        )


class ExecutionTopologySegmentValidationTests(unittest.TestCase):
    """L11-R1/R2/R6: cross-document node-kind legality and partition facts."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.coord = Path(self.temp.name)
        self.tasks = self.coord / "tasks" / REPOSITORY
        self.tasks.mkdir(parents=True)
        self.topology = TaskDocumentTopology(self.coord)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_segmented_sprint(
        self,
        *,
        nature_a: str = "organizational",
        leafs_a: list[str] | None = None,
    ) -> None:
        leafs = ["L1", "L2", "L3"] if leafs_a is None else leafs_a
        write_task_doc(
            self.tasks / "master-a",
            _master(identity="MASTER-A", execution_nature=nature_a).model_copy(
                update={
                    "subTasks": [
                        SubTaskRef(number=leaf, name=leaf, file=f"{leaf.lower()}.md")
                        for leaf in leafs
                    ]
                }
            ),
        )
        write_task_doc(
            self.tasks / "master-b",
            _master(identity="MASTER-B", execution_nature="atomic").model_copy(
                update={"subTasks": [SubTaskRef(number="L1", name="L1", file="l1.md")]}
            ),
        )
        write_task_doc(
            self.tasks / "sprint",
            _master(
                identity="SPRINT",
                orchestrates=["master-a", "master-b"],
                execution_graph={
                    "nodes": [
                        _segment(MASTER_A, ["L1"]),
                        _segment(MASTER_A, ["L2", "L3"]),
                        MASTER_B.model_dump(),
                    ],
                    "edges": [],
                },
            ),
        )

    def test_segmented_membership_matches_orchestrates_and_waves_run_over_nodes(self) -> None:
        self._write_segmented_sprint()
        masters = self.topology.validate_execution_topology(SPRINT)
        self.assertEqual([master.ref for master in masters], [MASTER_A, MASTER_B])
        waves = self.topology.execution_waves(SPRINT)
        # Waves run over nodes: both master-a segments land in wave 1.
        self.assertEqual(
            [[node.ref for node in wave] for wave in waves], [[MASTER_A, MASTER_A, MASTER_B]]
        )

    def test_segment_on_atomic_master_is_refused_citing_the_node(self) -> None:
        self._write_segmented_sprint(nature_a="atomic")
        with self.assertRaises(TaskDocumentRefError) as raised:
            self.topology.validate_execution_topology(SPRINT)
        self.assertEqual(raised.exception.status, "task-execution-graph-node-kind-invalid")
        self.assertIn(MASTER_A.key, str(raised.exception))
        self.assertIn("L1", str(raised.exception))
