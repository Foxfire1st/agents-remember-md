"""One-pass graph index for candidate-local semantic topology projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from agents_remember.errors import AgentsRememberError
from agents_remember.models.task_document_ref import TaskDocumentRef

from .document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
)
from .document_field_effects import (
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectProjector,
    TaskDocumentFieldEffectTaxonomyError,
)
from .execution_graph_validation import (
    ExecutionGraphValidationWork,
    minimum_successful_execution_graph_validation_work,
)
from .semantic_topology_graph_binding import immutable_semantic_topology_graph

MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK = 34_304


class SemanticTopologyGraphIndexError(AgentsRememberError):
    """An authored graph cannot supply one exact indexed candidate slice."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


@dataclass(frozen=True)
class SemanticTopologyGraphBuildWork:
    """Exact bounded collection operations performed while building one index."""

    nodeVisits: int
    leafPlacements: int
    nodeProjections: int
    edgeVisits: int
    endpointLookups: int
    edgeProjections: int
    incidentAttachments: int

    @property
    def total(self) -> int:
        return sum(
            (
                self.nodeVisits,
                self.leafPlacements,
                self.nodeProjections,
                self.edgeVisits,
                self.endpointLookups,
                self.edgeProjections,
                self.incidentAttachments,
            )
        )


@dataclass(frozen=True)
class SemanticTopologyCandidateWork:
    """Exact bounded index reads performed for one candidate placement."""

    placementLookups: int
    matchedNodeReads: int
    incidentEdgeReads: int

    @property
    def total(self) -> int:
        return self.placementLookups + self.matchedNodeReads + self.incidentEdgeReads


@dataclass(frozen=True)
class SemanticTopologyPopulationWork:
    """Exact bounded index reads for the complete authoritative leaf population."""

    candidateCount: int
    placementLookups: int
    matchedNodeReads: int
    incidentEdgeReads: int

    @property
    def total(self) -> int:
        return self.placementLookups + self.matchedNodeReads + self.incidentEdgeReads


@dataclass(frozen=True)
class SemanticTopologyGraphValidationWork:
    """One-time work that binds separately resolved copies of one authored graph.

    ``canonicalByteBlocks`` is the exact ceiling of encoded bytes divided by 1,024
    for every complete canonical-byte traversal, so large scalar payloads remain
    inside the budget. ``graphSnapshots`` counts the single deep-immutable context
    materialized from the already-captured authored bytes. ``graphAdmissions``
    counts its single canonical whole-graph invariant pass. Canonical source walks,
    immutable member materializations, and the schema-owned admission's indexed
    collection operations are separate and independently instrumentable.
    """

    graphCanonicalizations: int
    graphSnapshots: int
    graphAdmissions: int
    nodeVisits: int
    leafPlacements: int
    edgeVisits: int
    endpointVisits: int
    immutableNodeMaterializations: int
    immutableLeafMaterializations: int
    immutableEdgeMaterializations: int
    immutableEndpointMaterializations: int
    admissionWork: ExecutionGraphValidationWork
    canonicalByteBlocks: int
    graphComparisons: int

    @property
    def total(self) -> int:
        return sum(
            (
                self.graphCanonicalizations,
                self.graphSnapshots,
                self.graphAdmissions,
                self.nodeVisits,
                self.leafPlacements,
                self.edgeVisits,
                self.endpointVisits,
                self.immutableNodeMaterializations,
                self.immutableLeafMaterializations,
                self.immutableEdgeMaterializations,
                self.immutableEndpointMaterializations,
                self.admissionWork.total,
                self.canonicalByteBlocks,
                self.graphComparisons,
            )
        )


@dataclass(frozen=True)
class SemanticTopologyGraphSlice:
    """Preprojected structural facts incident to one exact authored node."""

    nodeOrder: int
    nodeProjection: bytes
    relevantEdgeProjections: tuple[bytes, ...]
    work: SemanticTopologyCandidateWork


@dataclass(frozen=True)
class _IncidentEdgeIndex:
    projections: tuple[tuple[bytes, ...], ...]
    graphError: str | None
    attachments: int


@dataclass(frozen=True)
class _ValidatedGraphBinding:
    sourceGraph: bytes
    boundGraph: SprintExecutionGraph
    work: SemanticTopologyGraphValidationWork


@dataclass(frozen=True)
class _WorkBudgetDetail:
    leafCount: int
    candidateCount: int
    incidentEdgeReads: int
    graphValidationWork: int
    preAdmission: bool = False


@dataclass(frozen=True)
class _GraphSnapshotAccounting:
    nodeCount: int
    leafCount: int
    edgeCount: int
    canonicalSize: int
    admissionWork: ExecutionGraphValidationWork


@dataclass(frozen=True)
class _GraphPopulationCounts:
    nodeCount: int
    leafCount: int
    edgeCount: int
    candidateCount: int
    canonicalSize: int


@dataclass(frozen=True)
class SemanticTopologyGraphIndex:
    """Immutable canonical graph projections and constant-time placement indexes."""

    sourceGraphDigest: str
    boundGraph: SprintExecutionGraph = field(repr=False, compare=False)
    nodeProjections: tuple[bytes, ...]
    incidentEdgeProjections: tuple[tuple[bytes, ...], ...]
    lumpNodes: Mapping[TaskDocumentRef, tuple[int, ...]]
    segmentLeafNodes: Mapping[tuple[TaskDocumentRef, str], tuple[int, ...]]
    graphError: str | None
    buildWork: SemanticTopologyGraphBuildWork
    populationWork: SemanticTopologyPopulationWork
    validationWork: SemanticTopologyGraphValidationWork

    @property
    def totalWork(self) -> int:
        return self.buildWork.total + self.populationWork.total + self.validationWork.total

    def is_bound_to(self, graph: SprintExecutionGraph) -> bool:
        """Return whether ``graph`` is the exact immutable consumer context."""

        return self.boundGraph is graph

    def candidate_slice(
        self,
        master_ref: TaskDocumentRef,
        leaf_id: str,
    ) -> SemanticTopologyGraphSlice:
        matches: tuple[int, ...] = tuple(self.lumpNodes.get(master_ref, ())) + tuple(
            self.segmentLeafNodes.get((master_ref, leaf_id), ())
        )
        work = SemanticTopologyCandidateWork(
            placementLookups=2,
            matchedNodeReads=len(matches),
            incidentEdgeReads=0,
        )
        if not matches:
            raise SemanticTopologyGraphIndexError(
                "semantic-topology-dag-placement-missing",
                "candidate has no exact node in the authored execution graph",
            )
        if len(matches) > 1:
            raise SemanticTopologyGraphIndexError(
                "semantic-topology-dag-placement-ambiguous",
                f"candidate resolves to {len(matches)} authored graph nodes",
            )
        if self.graphError is not None:
            raise SemanticTopologyGraphIndexError(
                "semantic-topology-graph-invalid",
                self.graphError,
            )
        node_index = next(iter(matches))
        edges = self.incidentEdgeProjections[node_index]
        return SemanticTopologyGraphSlice(
            nodeOrder=node_index,
            nodeProjection=self.nodeProjections[node_index],
            relevantEdgeProjections=edges,
            work=SemanticTopologyCandidateWork(
                placementLookups=work.placementLookups,
                matchedNodeReads=work.matchedNodeReads,
                incidentEdgeReads=len(edges),
            ),
        )


def build_semantic_topology_graph_index(
    graph: SprintExecutionGraph,
    candidate_leaf_ids: Mapping[TaskDocumentRef, tuple[str, ...]],
    *,
    authored_graph: SprintExecutionGraph,
) -> SemanticTopologyGraphIndex:
    """Project one graph and bind it once to the exact authored consumer context."""

    try:
        projector = TaskDocumentFieldEffectProjector()
        candidate_count = sum(len(leaf_ids) for leaf_ids in candidate_leaf_ids.values())
        binding = _validated_graph_binding(
            graph,
            authored_graph,
            candidate_count=candidate_count,
        )
        return _build_graph_index(
            candidate_leaf_ids,
            projector,
            binding,
            candidate_count,
        )
    except TaskDocumentFieldEffectTaxonomyError as exc:
        raise SemanticTopologyGraphIndexError(
            "semantic-topology-schema-unclassified",
            str(exc),
        ) from exc


def _build_graph_index(
    candidate_leaf_ids: Mapping[TaskDocumentRef, tuple[str, ...]],
    projector: TaskDocumentFieldEffectProjector,
    binding: _ValidatedGraphBinding,
    candidate_count: int,
) -> SemanticTopologyGraphIndex:
    graph = binding.boundGraph
    leaf_count = binding.work.immutableLeafMaterializations
    _require_minimum_work_budget(graph, leaf_count, candidate_count, binding.work)
    nodes_by_master, lump_nodes, leaf_nodes = _node_lookup_indexes(graph.nodes)
    node_projections = tuple(_project_structural(node, projector) for node in graph.nodes)
    edges = _incident_edge_index(graph, projector, nodes_by_master, leaf_nodes)
    build_work = SemanticTopologyGraphBuildWork(
        nodeVisits=len(graph.nodes),
        leafPlacements=leaf_count,
        nodeProjections=len(node_projections),
        edgeVisits=len(graph.edges),
        endpointLookups=2 * len(graph.edges),
        edgeProjections=len(graph.edges),
        incidentAttachments=edges.attachments,
    )
    population_work = _population_work(
        candidate_leaf_ids,
        lump_nodes,
        leaf_nodes,
        edges.projections,
    )
    _require_exact_work_budget(build_work, population_work, binding.work)
    return SemanticTopologyGraphIndex(
        sourceGraphDigest=hashlib.sha256(binding.sourceGraph).hexdigest(),
        boundGraph=binding.boundGraph,
        nodeProjections=node_projections,
        incidentEdgeProjections=edges.projections,
        lumpNodes=MappingProxyType({ref: tuple(indexes) for ref, indexes in lump_nodes.items()}),
        segmentLeafNodes=MappingProxyType(
            {key: tuple(indexes) for key, indexes in leaf_nodes.items()}
        ),
        graphError=edges.graphError,
        buildWork=build_work,
        populationWork=population_work,
        validationWork=binding.work,
    )


def _require_minimum_work_budget(
    graph: SprintExecutionGraph,
    leaf_count: int,
    candidate_count: int,
    validation_work: SemanticTopologyGraphValidationWork,
) -> None:
    minimum = (
        2 * len(graph.nodes)
        + leaf_count
        + 4 * len(graph.edges)
        + 2 * candidate_count
        + validation_work.total
    )
    if minimum > MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK:
        _raise_work_budget_exceeded(
            minimum,
            _WorkBudgetDetail(
                leafCount=leaf_count,
                candidateCount=candidate_count,
                incidentEdgeReads=0,
                graphValidationWork=validation_work.total,
            ),
        )


def _population_work(
    candidate_leaf_ids: Mapping[TaskDocumentRef, tuple[str, ...]],
    lump_nodes: Mapping[TaskDocumentRef, list[int]],
    leaf_nodes: Mapping[tuple[TaskDocumentRef, str], list[int]],
    incident_edges: tuple[tuple[bytes, ...], ...],
) -> SemanticTopologyPopulationWork:
    matched_node_reads = 0
    incident_edge_reads = 0
    candidate_count = 0
    for master_ref, leaf_ids in candidate_leaf_ids.items():
        lump_matches = lump_nodes.get(master_ref, ())
        for leaf_id in leaf_ids:
            candidate_count += 1
            matches = (*lump_matches, *leaf_nodes.get((master_ref, leaf_id), ()))
            matched_node_reads += len(matches)
            incident_edge_reads += sum(len(incident_edges[index]) for index in matches)
    return SemanticTopologyPopulationWork(
        candidateCount=candidate_count,
        placementLookups=2 * candidate_count,
        matchedNodeReads=matched_node_reads,
        incidentEdgeReads=incident_edge_reads,
    )


def _require_exact_work_budget(
    build_work: SemanticTopologyGraphBuildWork,
    population_work: SemanticTopologyPopulationWork,
    validation_work: SemanticTopologyGraphValidationWork,
) -> None:
    total = build_work.total + population_work.total + validation_work.total
    if total > MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK:
        _raise_work_budget_exceeded(
            total,
            _WorkBudgetDetail(
                leafCount=build_work.leafPlacements,
                candidateCount=population_work.candidateCount,
                incidentEdgeReads=population_work.incidentEdgeReads,
                graphValidationWork=validation_work.total,
            ),
        )


def _raise_work_budget_exceeded(
    observed: int,
    detail: _WorkBudgetDetail,
) -> None:
    basis = "pre-admission lower bound" if detail.preAdmission else "work"
    raise SemanticTopologyGraphIndexError(
        "semantic-topology-graph-work-budget-exceeded",
        f"semantic topology graph {basis} {observed} exceeds the enforced "
        f"{MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK}-unit budget "
        f"(leaf placements={detail.leafCount}, candidates={detail.candidateCount}, "
        f"candidate incident-edge reads={detail.incidentEdgeReads}, "
        f"one-time graph validation work={detail.graphValidationWork})",
    )


def _canonical_graph_capture(
    graph: SprintExecutionGraph,
) -> tuple[bytes, SemanticTopologyGraphValidationWork]:
    encoded = _canonical_graph_bytes(graph)
    return encoded, SemanticTopologyGraphValidationWork(
        graphCanonicalizations=1,
        graphSnapshots=0,
        graphAdmissions=0,
        nodeVisits=len(graph.nodes),
        leafPlacements=sum(len(node.leafIds) for node in graph.nodes),
        edgeVisits=len(graph.edges),
        endpointVisits=2 * len(graph.edges),
        immutableNodeMaterializations=0,
        immutableLeafMaterializations=0,
        immutableEdgeMaterializations=0,
        immutableEndpointMaterializations=0,
        admissionWork=ExecutionGraphValidationWork(),
        canonicalByteBlocks=max(1, (len(encoded) + 1023) // 1024),
        graphComparisons=0,
    )


def _validated_graph_binding(
    graph: SprintExecutionGraph,
    authored_graph: SprintExecutionGraph,
    *,
    candidate_count: int,
) -> _ValidatedGraphBinding:
    source_graph, source_work = _canonical_graph_capture(graph)
    if authored_graph is graph:
        authored = source_graph
        validation_work = _with_graph_comparison(source_work)
    else:
        authored, authored_work = _canonical_graph_capture(authored_graph)
        validation_work = _combine_validation_work(source_work, authored_work)
    if source_graph != authored:
        raise SemanticTopologyGraphIndexError(
            "semantic-topology-graph-context-mismatch",
            "the supplied graph differs from the sprint's authored graph",
        )
    population = _GraphPopulationCounts(
        nodeCount=source_work.nodeVisits,
        leafCount=source_work.leafPlacements,
        edgeCount=source_work.edgeVisits,
        candidateCount=candidate_count,
        canonicalSize=len(authored),
    )
    _require_pre_admission_work_budget(
        population,
        validation_work,
    )
    try:
        bound_graph = immutable_semantic_topology_graph(authored)
    except ValueError as exc:
        raise SemanticTopologyGraphIndexError(
            "semantic-topology-graph-invalid",
            "the validated authored graph could not form an immutable context",
        ) from exc
    return _ValidatedGraphBinding(
        source_graph,
        bound_graph,
        _with_graph_snapshot(validation_work, bound_graph, population),
    )


def _require_pre_admission_work_budget(
    population: _GraphPopulationCounts,
    work: SemanticTopologyGraphValidationWork,
) -> None:
    """Refuse when even a successful indexed admission cannot fit the budget."""

    admission_lower_bound = minimum_successful_execution_graph_validation_work(
        population.nodeCount,
        population.leafCount,
        population.edgeCount,
    )
    predicted_validation = _with_graph_snapshot_counts(
        work,
        _GraphSnapshotAccounting(
            nodeCount=population.nodeCount,
            leafCount=population.leafCount,
            edgeCount=population.edgeCount,
            canonicalSize=population.canonicalSize,
            admissionWork=admission_lower_bound,
        ),
    )
    minimum = (
        2 * population.nodeCount
        + population.leafCount
        + 4 * population.edgeCount
        + 2 * population.candidateCount
        + predicted_validation.total
    )
    if minimum > MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK:
        _raise_work_budget_exceeded(
            minimum,
            _WorkBudgetDetail(
                leafCount=population.leafCount,
                candidateCount=population.candidateCount,
                incidentEdgeReads=0,
                graphValidationWork=predicted_validation.total,
                preAdmission=True,
            ),
        )


def _with_graph_snapshot(
    work: SemanticTopologyGraphValidationWork,
    graph: SprintExecutionGraph,
    population: _GraphPopulationCounts,
) -> SemanticTopologyGraphValidationWork:
    """Count the sole immutable parse and its measured canonical admission."""

    return _with_graph_snapshot_counts(
        work,
        _GraphSnapshotAccounting(
            nodeCount=population.nodeCount,
            leafCount=population.leafCount,
            edgeCount=population.edgeCount,
            canonicalSize=population.canonicalSize,
            admissionWork=graph.execution_graph_validation_work,
        ),
    )


def _with_graph_snapshot_counts(
    work: SemanticTopologyGraphValidationWork,
    accounting: _GraphSnapshotAccounting,
) -> SemanticTopologyGraphValidationWork:
    return SemanticTopologyGraphValidationWork(
        graphCanonicalizations=work.graphCanonicalizations,
        graphSnapshots=work.graphSnapshots + 1,
        graphAdmissions=work.graphAdmissions + 1,
        nodeVisits=work.nodeVisits,
        leafPlacements=work.leafPlacements,
        edgeVisits=work.edgeVisits,
        endpointVisits=work.endpointVisits,
        immutableNodeMaterializations=work.immutableNodeMaterializations + accounting.nodeCount,
        immutableLeafMaterializations=work.immutableLeafMaterializations + accounting.leafCount,
        immutableEdgeMaterializations=work.immutableEdgeMaterializations + accounting.edgeCount,
        immutableEndpointMaterializations=(
            work.immutableEndpointMaterializations + 2 * accounting.edgeCount
        ),
        admissionWork=accounting.admissionWork,
        canonicalByteBlocks=(
            work.canonicalByteBlocks + max(1, (accounting.canonicalSize + 1023) // 1024)
        ),
        graphComparisons=work.graphComparisons,
    )


def _with_graph_comparison(
    work: SemanticTopologyGraphValidationWork,
) -> SemanticTopologyGraphValidationWork:
    return SemanticTopologyGraphValidationWork(
        graphCanonicalizations=work.graphCanonicalizations,
        graphSnapshots=work.graphSnapshots,
        graphAdmissions=work.graphAdmissions,
        nodeVisits=work.nodeVisits,
        leafPlacements=work.leafPlacements,
        edgeVisits=work.edgeVisits,
        endpointVisits=work.endpointVisits,
        immutableNodeMaterializations=work.immutableNodeMaterializations,
        immutableLeafMaterializations=work.immutableLeafMaterializations,
        immutableEdgeMaterializations=work.immutableEdgeMaterializations,
        immutableEndpointMaterializations=work.immutableEndpointMaterializations,
        admissionWork=work.admissionWork,
        canonicalByteBlocks=work.canonicalByteBlocks,
        graphComparisons=1,
    )


def _combine_validation_work(
    source: SemanticTopologyGraphValidationWork,
    authored: SemanticTopologyGraphValidationWork,
) -> SemanticTopologyGraphValidationWork:
    return SemanticTopologyGraphValidationWork(
        graphCanonicalizations=source.graphCanonicalizations + authored.graphCanonicalizations,
        graphSnapshots=source.graphSnapshots + authored.graphSnapshots,
        graphAdmissions=source.graphAdmissions + authored.graphAdmissions,
        nodeVisits=source.nodeVisits + authored.nodeVisits,
        leafPlacements=source.leafPlacements + authored.leafPlacements,
        edgeVisits=source.edgeVisits + authored.edgeVisits,
        endpointVisits=source.endpointVisits + authored.endpointVisits,
        immutableNodeMaterializations=(
            source.immutableNodeMaterializations + authored.immutableNodeMaterializations
        ),
        immutableLeafMaterializations=(
            source.immutableLeafMaterializations + authored.immutableLeafMaterializations
        ),
        immutableEdgeMaterializations=(
            source.immutableEdgeMaterializations + authored.immutableEdgeMaterializations
        ),
        immutableEndpointMaterializations=(
            source.immutableEndpointMaterializations + authored.immutableEndpointMaterializations
        ),
        admissionWork=ExecutionGraphValidationWork(),
        canonicalByteBlocks=source.canonicalByteBlocks + authored.canonicalByteBlocks,
        graphComparisons=1,
    )


def _incident_edge_index(
    graph: SprintExecutionGraph,
    projector: TaskDocumentFieldEffectProjector,
    nodes_by_master: Mapping[TaskDocumentRef, list[int]],
    leaf_nodes: Mapping[tuple[TaskDocumentRef, str], list[int]],
) -> _IncidentEdgeIndex:
    incident: list[list[bytes]] = [[] for _ in graph.nodes]
    graph_error: str | None = None
    attachments = 0
    for edge in graph.edges:
        edge_projection = _project_structural(edge, projector)
        predecessor = _endpoint_matches(edge.predecessor, nodes_by_master, leaf_nodes)
        successor = _endpoint_matches(edge.successor, nodes_by_master, leaf_nodes)
        if len(predecessor) != 1 or len(successor) != 1:
            graph_error = "an authored graph edge has invalid or ambiguous endpoints"
            continue
        attachments += _attach_incident_edge(
            incident,
            predecessor[0],
            successor[0],
            edge_projection,
        )
    return _IncidentEdgeIndex(
        projections=tuple(tuple(sorted(edges)) for edges in incident),
        graphError=graph_error,
        attachments=attachments,
    )


def _attach_incident_edge(
    incident: list[list[bytes]],
    predecessor: int,
    successor: int,
    projection: bytes,
) -> int:
    incident[predecessor].append(projection)
    if successor == predecessor:
        return 1
    incident[successor].append(projection)
    return 2


def _node_lookup_indexes(
    nodes: Sequence[SprintExecutionNode],
) -> tuple[
    dict[TaskDocumentRef, list[int]],
    dict[TaskDocumentRef, list[int]],
    dict[tuple[TaskDocumentRef, str], list[int]],
]:
    by_master: dict[TaskDocumentRef, list[int]] = {}
    lump_nodes: dict[TaskDocumentRef, list[int]] = {}
    by_leaf: dict[tuple[TaskDocumentRef, str], list[int]] = {}
    for index, node in enumerate(nodes):
        by_master.setdefault(node.ref, []).append(index)
        if node.kind == "master":
            lump_nodes.setdefault(node.ref, []).append(index)
        for leaf_id in node.leafIds:
            by_leaf.setdefault((node.ref, leaf_id), []).append(index)
    return by_master, lump_nodes, by_leaf


def _endpoint_matches(
    endpoint: TaskDocumentRef | SprintExecutionEndpoint,
    nodes_by_master: Mapping[TaskDocumentRef, list[int]],
    leaf_nodes: Mapping[tuple[TaskDocumentRef, str], list[int]],
) -> tuple[int, ...]:
    if isinstance(endpoint, SprintExecutionEndpoint) and endpoint.leafId is not None:
        return tuple(leaf_nodes.get((endpoint.ref, endpoint.leafId), ()))
    ref = endpoint.ref if isinstance(endpoint, SprintExecutionEndpoint) else endpoint
    return tuple(nodes_by_master.get(ref, ()))


def _project_structural(
    source: SprintExecutionNode | SprintExecutionEdge,
    projector: TaskDocumentFieldEffectProjector,
) -> bytes:
    value = projector.project(source, TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_graph_bytes(graph: SprintExecutionGraph) -> bytes:
    value = graph.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK",
    "SemanticTopologyCandidateWork",
    "SemanticTopologyGraphBuildWork",
    "SemanticTopologyGraphIndex",
    "SemanticTopologyGraphIndexError",
    "SemanticTopologyGraphSlice",
    "SemanticTopologyGraphValidationWork",
    "SemanticTopologyPopulationWork",
    "build_semantic_topology_graph_index",
]
