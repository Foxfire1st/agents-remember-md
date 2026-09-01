"""Exact refusal coverage for semantic-topology projection internals."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from agents_remember.tasks import semantic_topology as topology_module
from agents_remember.tasks import semantic_topology_graph as graph_module
from agents_remember.tasks.document import SprintExecutionGraph
from agents_remember.tasks.document_field_effects import (
    TaskDocumentFieldEffectTaxonomyError,
)
from agents_remember.tasks.execution_graph_validation import ExecutionGraphValidationWork
from agents_remember.tasks.leaf_binding import CanonicalLeafBindingError
from agents_remember.tasks.semantic_topology import SemanticTopologyError
from agents_remember.tasks.semantic_topology_graph import (
    SemanticTopologyCandidateWork,
    SemanticTopologyGraphBuildWork,
    SemanticTopologyGraphIndexError,
    SemanticTopologyGraphSlice,
    SemanticTopologyGraphValidationWork,
    SemanticTopologyPopulationWork,
    build_semantic_topology_graph_index,
)
from agents_remember.tasks.semantic_topology_graph_binding import (
    _ImmutableSprintExecutionNode,
)
from pydantic import ValidationError
from test_closeout_projection_member_helpers import MASTER, _documents


def _zero_validation_work() -> SemanticTopologyGraphValidationWork:
    return SemanticTopologyGraphValidationWork(
        graphCanonicalizations=0,
        graphSnapshots=0,
        graphAdmissions=0,
        nodeVisits=0,
        leafPlacements=0,
        edgeVisits=0,
        endpointVisits=0,
        immutableNodeMaterializations=0,
        immutableLeafMaterializations=0,
        immutableEdgeMaterializations=0,
        immutableEndpointMaterializations=0,
        admissionWork=ExecutionGraphValidationWork(),
        canonicalByteBlocks=0,
        graphComparisons=0,
    )


class SemanticTopologyCoverageEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sprint, self.master, self.candidate = _documents()
        self.graph = cast(SprintExecutionGraph, self.sprint.document.executionGraph)
        self.index = build_semantic_topology_graph_index(
            self.graph,
            {MASTER: tuple(row.number for row in self.master.document.subTasks)},
            authored_graph=self.graph,
        )

    def test_graphless_projection_rejects_a_dag_context(self) -> None:
        graphless = replace(
            self.sprint,
            document=self.sprint.document.model_copy(update={"executionGraph": None}),
        )
        with self.assertRaises(SemanticTopologyError) as raised:
            topology_module.semantic_topology_projection(
                graphless,
                self.master,
                self.candidate,
                graph_index=self.index,
            )
        self.assertEqual(raised.exception.status, "semantic-topology-graph-context-mismatch")
        self.assertEqual(
            raised.exception.detail,
            "graph-less atomic-sequential scheduling received a DAG context",
        )

    def test_unknown_leaf_binding_status_is_not_translated_by_default(self) -> None:
        graphless = replace(
            self.sprint,
            document=self.sprint.document.model_copy(update={"executionGraph": None}),
        )
        with (
            mock.patch.object(
                topology_module,
                "require_canonical_leaf_binding",
                side_effect=CanonicalLeafBindingError("future-status", "future detail"),
            ),
            self.assertRaises(SemanticTopologyError) as raised,
        ):
            topology_module.semantic_topology_projection(
                graphless,
                self.master,
                self.candidate,
                graph_index=None,
            )
        self.assertEqual(raised.exception.status, "semantic-topology-schema-unclassified")
        self.assertEqual(
            raised.exception.detail,
            "unsupported canonical leaf-binding status 'future-status'",
        )

    def test_invalid_indexed_projection_is_a_typed_schema_refusal(self) -> None:
        row = self.master.document.subTasks[0]
        invalid_slice = SemanticTopologyGraphSlice(
            nodeOrder=0,
            nodeProjection=b"{}",
            relevantEdgeProjections=(),
            work=SemanticTopologyCandidateWork(
                placementLookups=2,
                matchedNodeReads=1,
                incidentEdgeReads=0,
            ),
        )
        fake_index = cast(
            Any,
            SimpleNamespace(candidate_slice=mock.Mock(return_value=invalid_slice)),
        )
        with self.assertRaises(SemanticTopologyError) as raised:
            topology_module._dag_placement(fake_index, MASTER, row)
        self.assertEqual(raised.exception.status, "semantic-topology-schema-unclassified")
        self.assertEqual(
            raised.exception.detail,
            "indexed structural graph projection is not fully consumed",
        )

    def test_structural_projection_translates_taxonomy_and_shape_failures(self) -> None:
        taxonomy_projector = cast(
            Any,
            SimpleNamespace(
                project=mock.Mock(
                    side_effect=TaskDocumentFieldEffectTaxonomyError("missing classification")
                )
            ),
        )
        with self.assertRaises(SemanticTopologyError) as raised:
            topology_module._structural_projection(
                topology_module.SemanticTopologyRef,
                self.candidate.ref,
                taxonomy_projector,
            )
        self.assertEqual(raised.exception.status, "semantic-topology-schema-unclassified")
        self.assertEqual(raised.exception.detail, "missing classification")

        invalid_projector = cast(
            Any,
            SimpleNamespace(project=mock.Mock(return_value={"repository": "only"})),
        )
        with self.assertRaises(SemanticTopologyError) as raised:
            topology_module._structural_projection(
                topology_module.SemanticTopologyRef,
                self.candidate.ref,
                invalid_projector,
            )
        self.assertEqual(raised.exception.status, "semantic-topology-schema-unclassified")
        self.assertEqual(
            raised.exception.detail,
            "structural projection for TaskDocumentRef is not fully consumed",
        )

    def test_candidate_slice_refuses_ambiguous_index_membership(self) -> None:
        ambiguous = replace(
            self.index,
            lumpNodes={MASTER: (0,)},
            segmentLeafNodes={(MASTER, self.master.document.subTasks[0].number): (1,)},
        )
        with self.assertRaises(SemanticTopologyGraphIndexError) as raised:
            ambiguous.candidate_slice(MASTER, self.master.document.subTasks[0].number)
        self.assertEqual(raised.exception.status, "semantic-topology-dag-placement-ambiguous")
        self.assertEqual(
            raised.exception.detail,
            "candidate resolves to 2 authored graph nodes",
        )

    def test_graph_index_translates_taxonomy_failure(self) -> None:
        with (
            mock.patch.object(
                graph_module,
                "TaskDocumentFieldEffectProjector",
                side_effect=TaskDocumentFieldEffectTaxonomyError("taxonomy unavailable"),
            ),
            self.assertRaises(SemanticTopologyGraphIndexError) as raised,
        ):
            build_semantic_topology_graph_index(
                self.graph,
                {MASTER: (self.master.document.subTasks[0].number,)},
                authored_graph=self.graph,
            )
        self.assertEqual(raised.exception.status, "semantic-topology-schema-unclassified")
        self.assertEqual(raised.exception.detail, "taxonomy unavailable")

    def test_both_post_capture_work_budget_guards_refuse(self) -> None:
        build_work = SemanticTopologyGraphBuildWork(
            nodeVisits=1,
            leafPlacements=0,
            nodeProjections=1,
            edgeVisits=0,
            endpointLookups=0,
            edgeProjections=0,
            incidentAttachments=0,
        )
        population_work = SemanticTopologyPopulationWork(
            candidateCount=1,
            placementLookups=2,
            matchedNodeReads=1,
            incidentEdgeReads=0,
        )
        with (
            mock.patch.object(graph_module, "MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK", 0),
            self.assertRaises(SemanticTopologyGraphIndexError) as raised,
        ):
            graph_module._require_minimum_work_budget(
                self.graph,
                0,
                0,
                _zero_validation_work(),
            )
        self.assertEqual(raised.exception.status, "semantic-topology-graph-work-budget-exceeded")
        self.assertEqual(
            raised.exception.detail,
            "semantic topology graph work 14 exceeds the enforced 0-unit budget "
            "(leaf placements=0, candidates=0, candidate incident-edge reads=0, "
            "one-time graph validation work=0)",
        )
        with (
            mock.patch.object(graph_module, "MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK", 0),
            self.assertRaises(SemanticTopologyGraphIndexError) as raised,
        ):
            graph_module._require_exact_work_budget(
                build_work,
                population_work,
                _zero_validation_work(),
            )
        self.assertEqual(raised.exception.status, "semantic-topology-graph-work-budget-exceeded")
        self.assertEqual(
            raised.exception.detail,
            "semantic topology graph work 5 exceeds the enforced 0-unit budget "
            "(leaf placements=0, candidates=1, candidate incident-edge reads=0, "
            "one-time graph validation work=0)",
        )

    def test_self_incident_edge_attaches_once_and_blank_leaf_refuses(self) -> None:
        incident: list[list[bytes]] = [[]]
        self.assertEqual(graph_module._attach_incident_edge(incident, 0, 0, b"edge"), 1)
        self.assertEqual(incident, [[b"edge"]])

        with self.assertRaisesRegex(ValidationError, "leafIds must not be blank"):
            _ImmutableSprintExecutionNode(ref=MASTER, leafIds=(" ",))
