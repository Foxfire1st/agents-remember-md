"""Direct operation-count regressions for canonical indexed graph admission."""

from __future__ import annotations

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import document as document_module
from agents_remember.tasks import semantic_topology_graph as graph_module
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
)
from agents_remember.tasks.execution_graph_validation import ExecutionGraphValidationWork
from agents_remember.tasks.semantic_topology_graph import (
    MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK,
    SemanticTopologyGraphIndexError,
    build_semantic_topology_graph_index,
)

MASTER = TaskDocumentRef(repository="repo", path="master/task.json")


def _dense_graph(size: int) -> tuple[SprintExecutionGraph, tuple[str, ...]]:
    leaf_ids = tuple(f"L{index:03d}" for index in range(size))
    nodes = [
        SprintExecutionNode(kind="segment", ref=MASTER, leafIds=[leaf_id]) for leaf_id in leaf_ids
    ]
    edges = [
        SprintExecutionEdge(
            predecessor=SprintExecutionEndpoint(ref=MASTER, leafId=leaf_ids[left]),
            successor=SprintExecutionEndpoint(ref=MASTER, leafId=leaf_ids[right]),
            reason="Declared scheduling dependency.",
        )
        for left in range(size)
        for right in range(left + 1, size)
    ]
    return SprintExecutionGraph(nodes=nodes, edges=edges), leaf_ids


def _expected_dense_work(size: int) -> ExecutionGraphValidationWork:
    edge_count = size * (size - 1) // 2
    return ExecutionGraphValidationWork(
        nodeUniquenessChecks=size,
        leafIdentityReads=size,
        nodeRefIndexAttachments=size,
        masterOwnershipChecks=1,
        leafOwnershipChecks=size,
        leafEndpointIndexAttachments=size,
        edgeVisits=edge_count,
        endpointIndexLookups=2 * edge_count,
        resolvedEdgeUniquenessChecks=edge_count,
        indegreeInitializations=size,
        dependentListInitializations=size,
        dependencyAttachments=edge_count,
        indegreeIncrements=edge_count,
        readyNodeChecks=size,
        waveNodeVisits=size,
        dependentEdgeVisits=edge_count,
        indegreeDecrements=edge_count,
        dependentReadyChecks=edge_count,
    )


def test_dense_admission_uses_indexed_operations_and_never_public_resolver_scans() -> None:
    populations = []
    for size in (8, 32):
        authored, leaf_ids = _dense_graph(size)
        separately_resolved = SprintExecutionGraph.model_validate(authored.model_dump(mode="json"))
        populations.append((size, authored, separately_resolved, leaf_ids))

    resolver_calls = 0
    original_resolver = document_module.resolve_graph_endpoint

    def _traced_resolver(*args: object, **kwargs: object) -> SprintExecutionNode:
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    document_module.resolve_graph_endpoint = _traced_resolver
    try:
        for size, authored, separately_resolved, leaf_ids in populations:
            index = build_semantic_topology_graph_index(
                separately_resolved,
                {MASTER: leaf_ids},
                authored_graph=authored,
            )
            admission = index.validationWork.admissionWork
            assert admission == _expected_dense_work(size)
            assert admission.endpointIndexLookups == 2 * len(authored.edges)
            assert admission.resolvedEdgeUniquenessChecks == len(authored.edges)
            assert admission.dependencyAttachments == len(authored.edges)
            assert admission.dependentEdgeVisits == len(authored.edges)
            assert index.validationWork.immutableNodeMaterializations == size
            assert index.validationWork.immutableLeafMaterializations == size
            assert index.validationWork.immutableEdgeMaterializations == len(authored.edges)
            assert index.validationWork.immutableEndpointMaterializations == 2 * len(authored.edges)
            for leaf_id in leaf_ids:
                index.candidate_slice(MASTER, leaf_id)
            assert index.validationWork.graphCanonicalizations == 2
    finally:
        document_module.resolve_graph_endpoint = original_resolver

    assert resolver_calls == 0


def test_dense_over_budget_population_refuses_before_immutable_admission() -> None:
    graph, leaf_ids = _dense_graph(64)
    immutable_admissions = 0
    resolver_calls = 0
    original_snapshot = graph_module.immutable_semantic_topology_graph
    original_resolver = document_module.resolve_graph_endpoint

    def _unexpected_snapshot(canonical_graph: bytes) -> SprintExecutionGraph:
        nonlocal immutable_admissions
        immutable_admissions += 1
        return original_snapshot(canonical_graph)

    def _traced_resolver(*args: object, **kwargs: object) -> SprintExecutionNode:
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    graph_module.immutable_semantic_topology_graph = _unexpected_snapshot
    document_module.resolve_graph_endpoint = _traced_resolver
    try:
        try:
            build_semantic_topology_graph_index(
                graph,
                {MASTER: leaf_ids},
                authored_graph=graph,
            )
        except SemanticTopologyGraphIndexError as exc:
            assert exc.status == "semantic-topology-graph-work-budget-exceeded"
            assert "pre-admission lower bound" in exc.detail
            observed = int(exc.detail.split("pre-admission lower bound ", 1)[1].split()[0])
            assert observed > MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK
        else:
            raise AssertionError("the over-budget dense graph must refuse")
    finally:
        graph_module.immutable_semantic_topology_graph = original_snapshot
        document_module.resolve_graph_endpoint = original_resolver

    assert immutable_admissions == 0
    assert resolver_calls == 0
    assert graph.execution_graph_validation_work == _expected_dense_work(64)
