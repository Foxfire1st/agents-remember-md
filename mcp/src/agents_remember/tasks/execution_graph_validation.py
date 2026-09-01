"""Indexed intrinsic validation for the persisted sprint execution graph.

The task-document schema owns admission.  This module is its internal algorithm:
one endpoint index and one resolved-edge population feed uniqueness, DAG, wave,
and cycle checks.  Keeping the algorithm outside ``document.py`` prevents that
already-large schema module from absorbing another independent responsibility.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Never, Protocol, cast

from agents_remember.models.task_document_ref import TaskDocumentRef


class _ExecutionGraphEndpoint(Protocol):
    @property
    def ref(self) -> TaskDocumentRef: ...

    @property
    def leafId(self) -> str | None: ...


class _ExecutionGraphNode(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def ref(self) -> TaskDocumentRef: ...

    @property
    def leafIds(self) -> Sequence[str]: ...

    def __hash__(self) -> int: ...


class _ExecutionGraphEdge(Protocol):
    @property
    def predecessor(self) -> TaskDocumentRef | _ExecutionGraphEndpoint: ...

    @property
    def successor(self) -> TaskDocumentRef | _ExecutionGraphEndpoint: ...


@dataclass(frozen=True)
class ExecutionGraphValidationWork:
    """Exact collection operations in one canonical whole-graph admission."""

    nodeUniquenessChecks: int = 0
    leafIdentityReads: int = 0
    nodeRefIndexAttachments: int = 0
    masterOwnershipChecks: int = 0
    leafOwnershipChecks: int = 0
    leafEndpointIndexAttachments: int = 0
    edgeVisits: int = 0
    endpointIndexLookups: int = 0
    resolvedEdgeUniquenessChecks: int = 0
    indegreeInitializations: int = 0
    dependentListInitializations: int = 0
    dependencyAttachments: int = 0
    indegreeIncrements: int = 0
    readyNodeChecks: int = 0
    waveNodeVisits: int = 0
    dependentEdgeVisits: int = 0
    indegreeDecrements: int = 0
    dependentReadyChecks: int = 0
    residualNodeChecks: int = 0
    cycleNodeVisits: int = 0
    cycleEdgeVisits: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.nodeUniquenessChecks,
                self.leafIdentityReads,
                self.nodeRefIndexAttachments,
                self.masterOwnershipChecks,
                self.leafOwnershipChecks,
                self.leafEndpointIndexAttachments,
                self.edgeVisits,
                self.endpointIndexLookups,
                self.resolvedEdgeUniquenessChecks,
                self.indegreeInitializations,
                self.dependentListInitializations,
                self.dependencyAttachments,
                self.indegreeIncrements,
                self.readyNodeChecks,
                self.waveNodeVisits,
                self.dependentEdgeVisits,
                self.indegreeDecrements,
                self.dependentReadyChecks,
                self.residualNodeChecks,
                self.cycleNodeVisits,
                self.cycleEdgeVisits,
            )
        )


@dataclass(frozen=True)
class ExecutionGraphAnalysis[NodeT: _ExecutionGraphNode]:
    """The one resolved population and waves produced by canonical admission."""

    resolvedEdgeIndexes: tuple[tuple[int, int], ...]
    waves: tuple[tuple[NodeT, ...], ...]
    work: ExecutionGraphValidationWork


class ExecutionGraphValidationError(ValueError):
    """An intrinsic graph error carrying work completed before refusal."""

    def __init__(self, detail: str, work: ExecutionGraphValidationWork) -> None:
        self.detail = detail
        self.work = work
        super().__init__(detail)


@dataclass
class _WorkCounter:
    nodeUniquenessChecks: int = 0
    leafIdentityReads: int = 0
    nodeRefIndexAttachments: int = 0
    masterOwnershipChecks: int = 0
    leafOwnershipChecks: int = 0
    leafEndpointIndexAttachments: int = 0
    edgeVisits: int = 0
    endpointIndexLookups: int = 0
    resolvedEdgeUniquenessChecks: int = 0
    indegreeInitializations: int = 0
    dependentListInitializations: int = 0
    dependencyAttachments: int = 0
    indegreeIncrements: int = 0
    readyNodeChecks: int = 0
    waveNodeVisits: int = 0
    dependentEdgeVisits: int = 0
    indegreeDecrements: int = 0
    dependentReadyChecks: int = 0
    residualNodeChecks: int = 0
    cycleNodeVisits: int = 0
    cycleEdgeVisits: int = 0

    def freeze(self) -> ExecutionGraphValidationWork:
        return ExecutionGraphValidationWork(
            nodeUniquenessChecks=self.nodeUniquenessChecks,
            leafIdentityReads=self.leafIdentityReads,
            nodeRefIndexAttachments=self.nodeRefIndexAttachments,
            masterOwnershipChecks=self.masterOwnershipChecks,
            leafOwnershipChecks=self.leafOwnershipChecks,
            leafEndpointIndexAttachments=self.leafEndpointIndexAttachments,
            edgeVisits=self.edgeVisits,
            endpointIndexLookups=self.endpointIndexLookups,
            resolvedEdgeUniquenessChecks=self.resolvedEdgeUniquenessChecks,
            indegreeInitializations=self.indegreeInitializations,
            dependentListInitializations=self.dependentListInitializations,
            dependencyAttachments=self.dependencyAttachments,
            indegreeIncrements=self.indegreeIncrements,
            readyNodeChecks=self.readyNodeChecks,
            waveNodeVisits=self.waveNodeVisits,
            dependentEdgeVisits=self.dependentEdgeVisits,
            indegreeDecrements=self.indegreeDecrements,
            dependentReadyChecks=self.dependentReadyChecks,
            residualNodeChecks=self.residualNodeChecks,
            cycleNodeVisits=self.cycleNodeVisits,
            cycleEdgeVisits=self.cycleEdgeVisits,
        )


@dataclass(frozen=True)
class _ResolvedGraph[NodeT: _ExecutionGraphNode]:
    resolvedEdgeIndexes: tuple[tuple[int, int], ...]
    nodeIdentityIndex: dict[tuple[str, TaskDocumentRef, tuple[str, ...]], int]


@dataclass
class _CycleSearch:
    stack: list[int]
    onStack: set[int]
    visited: set[int]


@dataclass(frozen=True)
class _CycleContext:
    successors: list[list[int]]
    residual: set[int]
    search: _CycleSearch
    counter: _WorkCounter


def validate_execution_graph[NodeT: _ExecutionGraphNode](
    nodes: Sequence[NodeT],
    edges: Sequence[_ExecutionGraphEdge],
) -> ExecutionGraphAnalysis[NodeT]:
    """Validate once using one endpoint index and one resolved-edge population."""

    counter = _WorkCounter()
    resolved = _resolve_graph(nodes, edges, counter)
    waves = _derive_waves(nodes, resolved, counter)
    return ExecutionGraphAnalysis(
        resolvedEdgeIndexes=resolved.resolvedEdgeIndexes,
        waves=waves,
        work=counter.freeze(),
    )


def find_execution_graph_cycle[NodeT: _ExecutionGraphNode](
    nodes: Sequence[NodeT],
    edges: Sequence[_ExecutionGraphEdge],
    residual: Sequence[NodeT],
) -> tuple[NodeT, ...]:
    """Compatibility seam for the historical deterministic cycle helper."""

    if not residual:
        return ()
    counter = _WorkCounter()
    resolved = _resolve_graph(nodes, edges, counter)
    _, successors = _dependency_state(len(nodes), resolved.resolvedEdgeIndexes, counter)
    residual_indexes = tuple(
        resolved.nodeIdentityIndex[_node_identity(node, counter=None)] for node in residual
    )
    cycle = _find_cycle_members(residual_indexes, successors, counter)
    return tuple(nodes[index] for index in cycle)


def minimum_successful_execution_graph_validation_work(
    node_count: int,
    leaf_count: int,
    edge_count: int,
) -> ExecutionGraphValidationWork:
    """Conservative work lower bound for a successful canonical admission.

    It intentionally assumes one owning-master group.  A real graph may have more,
    while every successfully admitted DAG performs every other listed operation.
    """

    return ExecutionGraphValidationWork(
        nodeUniquenessChecks=node_count,
        leafIdentityReads=leaf_count,
        nodeRefIndexAttachments=node_count,
        masterOwnershipChecks=int(node_count > 0),
        leafOwnershipChecks=leaf_count,
        leafEndpointIndexAttachments=leaf_count,
        edgeVisits=edge_count,
        endpointIndexLookups=2 * edge_count,
        resolvedEdgeUniquenessChecks=edge_count,
        indegreeInitializations=node_count,
        dependentListInitializations=node_count,
        dependencyAttachments=edge_count,
        indegreeIncrements=edge_count,
        readyNodeChecks=node_count,
        waveNodeVisits=node_count,
        dependentEdgeVisits=edge_count,
        indegreeDecrements=edge_count,
        dependentReadyChecks=edge_count,
    )


def _resolve_graph[NodeT: _ExecutionGraphNode](
    nodes: Sequence[NodeT],
    edges: Sequence[_ExecutionGraphEdge],
    counter: _WorkCounter,
) -> _ResolvedGraph[NodeT]:
    counter.nodeUniquenessChecks += len(nodes)
    node_identity_index: dict[tuple[str, TaskDocumentRef, tuple[str, ...]], int] = {}
    duplicate_node = False
    for index, node in enumerate(nodes):
        identity = _node_identity(node, counter)
        if identity in node_identity_index:
            duplicate_node = True
        else:
            node_identity_index[identity] = index
    if duplicate_node:
        _refuse("execution-graph nodes must be unique", counter)

    by_ref, by_leaf = _build_endpoint_index(nodes, counter)
    resolved_edges = _resolve_edges(edges, by_ref, by_leaf, counter)
    return _ResolvedGraph(resolved_edges, node_identity_index)


def _node_identity(
    node: _ExecutionGraphNode,
    counter: _WorkCounter | None,
) -> tuple[str, TaskDocumentRef, tuple[str, ...]]:
    if counter is not None:
        counter.leafIdentityReads += len(node.leafIds)
    return node.kind, node.ref, tuple(node.leafIds)


def _build_endpoint_index[NodeT: _ExecutionGraphNode](
    nodes: Sequence[NodeT],
    counter: _WorkCounter,
) -> tuple[
    dict[TaskDocumentRef, list[int]],
    dict[tuple[TaskDocumentRef, str], list[int]],
]:
    """Build the one ref/leaf endpoint index after ownership validation."""

    by_ref: dict[TaskDocumentRef, list[int]] = {}
    lump_refs: set[TaskDocumentRef] = set()
    for index, node in enumerate(nodes):
        by_ref.setdefault(node.ref, []).append(index)
        if node.kind == "master":
            lump_refs.add(node.ref)
        counter.nodeRefIndexAttachments += 1
    for ref, owned_indexes in by_ref.items():
        counter.masterOwnershipChecks += 1
        if len(owned_indexes) > 1 and ref in lump_refs:
            _refuse(
                "execution-graph lump and segment appearances of one master are mutually "
                f"exclusive: {ref.key}",
                counter,
            )

    placed: dict[str, TaskDocumentRef] = {}
    by_leaf: dict[tuple[TaskDocumentRef, str], list[int]] = {}
    for index, node in enumerate(nodes):
        for leaf in node.leafIds:
            counter.leafOwnershipChecks += 1
            owner = placed.get(leaf)
            if owner is not None:
                _refuse(
                    f"execution-graph leaf {leaf!r} is placed in more than one node "
                    f"({owner.key} and {node.ref.key})",
                    counter,
                )
            placed[leaf] = node.ref
            by_leaf.setdefault((node.ref, leaf), []).append(index)
            counter.leafEndpointIndexAttachments += 1
    return by_ref, by_leaf


def _resolve_edges(
    edges: Sequence[_ExecutionGraphEdge],
    by_ref: dict[TaskDocumentRef, list[int]],
    by_leaf: dict[tuple[TaskDocumentRef, str], list[int]],
    counter: _WorkCounter,
) -> tuple[tuple[int, int], ...]:
    resolved_edges: list[tuple[int, int]] = []
    for edge in edges:
        counter.edgeVisits += 1
        predecessor = _resolve_endpoint(edge.predecessor, by_ref, by_leaf, counter)
        successor = _resolve_endpoint(edge.successor, by_ref, by_leaf, counter)
        if predecessor == successor:
            _refuse("execution-graph edge cannot point a node to itself", counter)
        resolved_edges.append((predecessor, successor))
    counter.resolvedEdgeUniquenessChecks += len(resolved_edges)
    if len(set(resolved_edges)) != len(resolved_edges):
        _refuse("execution-graph edges must be unique", counter)
    return tuple(resolved_edges)


def _resolve_endpoint(
    endpoint: TaskDocumentRef | _ExecutionGraphEndpoint,
    by_ref: dict[TaskDocumentRef, list[int]],
    by_leaf: dict[tuple[TaskDocumentRef, str], list[int]],
    counter: _WorkCounter,
) -> int:
    counter.endpointIndexLookups += 1
    if isinstance(endpoint, TaskDocumentRef):
        ref = endpoint
        leaf = None
    else:
        ref = endpoint.ref
        leaf = endpoint.leafId
    matches: Sequence[int] = (
        by_leaf.get((ref, leaf), ()) if leaf is not None else by_ref.get(ref, ())
    )
    if not matches:
        if leaf is not None:
            _refuse(
                f"execution-graph endpoint leaf {leaf!r} is not placed in any node of {ref.key}",
                counter,
            )
        _refuse(f"execution-graph edge endpoint must be a declared node: {ref.key}", counter)
    if len(matches) > 1:
        _refuse(
            f"execution-graph edge endpoint {ref.key} is ambiguous; "
            "name a leafId of the target segment",
            counter,
        )
    return matches[0]


def _derive_waves[NodeT: _ExecutionGraphNode](
    nodes: Sequence[NodeT],
    resolved: _ResolvedGraph[NodeT],
    counter: _WorkCounter,
) -> tuple[tuple[NodeT, ...], ...]:
    indegree, successors = _dependency_state(
        len(nodes),
        resolved.resolvedEdgeIndexes,
        counter,
    )
    ready: list[int] = []
    for index in range(len(nodes)):
        counter.readyNodeChecks += 1
        if indegree[index] == 0:
            ready.append(index)

    waves: list[tuple[NodeT, ...]] = []
    visited = 0
    while ready:
        wave_indexes = tuple(sorted(ready))
        waves.append(tuple(nodes[index] for index in wave_indexes))
        visited += len(wave_indexes)
        next_ready: list[int] = []
        for node_index in wave_indexes:
            counter.waveNodeVisits += 1
            for successor in successors[node_index]:
                counter.dependentEdgeVisits += 1
                indegree[successor] -= 1
                counter.indegreeDecrements += 1
                counter.dependentReadyChecks += 1
                if indegree[successor] == 0:
                    next_ready.append(successor)
        ready = next_ready
    if visited != len(nodes):
        residual: list[int] = []
        for index in range(len(nodes)):
            counter.residualNodeChecks += 1
            if indegree[index] > 0:
                residual.append(index)
        cycle = _find_cycle_members(residual, successors, counter)
        _refuse(
            "execution-graph must be acyclic; cycle members: "
            + " -> ".join(nodes[index].ref.key for index in cycle),
            counter,
        )
    return tuple(waves)


def _dependency_state(
    node_count: int,
    resolved_edges: Sequence[tuple[int, int]],
    counter: _WorkCounter,
) -> tuple[list[int], list[list[int]]]:
    indegree: list[int] = []
    successors: list[list[int]] = []
    for _ in range(node_count):
        indegree.append(0)
        successors.append([])
        counter.indegreeInitializations += 1
        counter.dependentListInitializations += 1
    for predecessor, successor in resolved_edges:
        successors[predecessor].append(successor)
        counter.dependencyAttachments += 1
        indegree[successor] += 1
        counter.indegreeIncrements += 1
    return indegree, successors


def _find_cycle_members(
    residual: Sequence[int],
    successors: list[list[int]],
    counter: _WorkCounter,
) -> tuple[int, ...]:
    if not residual:
        return ()
    residual_set = set(residual)
    search = _CycleSearch(stack=[], onStack=set(), visited=set())
    context = _CycleContext(
        successors=successors,
        residual=residual_set,
        search=search,
        counter=counter,
    )
    for node_index in sorted(residual):
        if node_index in search.visited:
            continue
        found = _dfs_cycle_members(node_index, context)
        if found is not None:
            return found
    return tuple(residual)


def _dfs_cycle_members(node_index: int, context: _CycleContext) -> tuple[int, ...] | None:
    context.counter.cycleNodeVisits += 1
    context.search.stack.append(node_index)
    context.search.onStack.add(node_index)
    for successor in sorted(context.successors[node_index]):
        context.counter.cycleEdgeVisits += 1
        if successor in context.search.onStack:
            cycle_start = context.search.stack.index(successor)
            return tuple(context.search.stack[cycle_start:])
        if successor in context.residual and successor not in context.search.visited:
            found = _dfs_cycle_members(successor, context)
            if found is not None:
                return found
    context.search.stack.pop()
    context.search.onStack.discard(node_index)
    context.search.visited.add(node_index)
    return None


def _refuse(detail: str, counter: _WorkCounter) -> Never:
    raise ExecutionGraphValidationError(detail, counter.freeze())


def as_execution_graph_edges(
    edges: Sequence[object],
) -> Sequence[_ExecutionGraphEdge]:
    """Narrow Pydantic edge collections at the schema-owned call boundary."""

    return cast(Sequence[_ExecutionGraphEdge], edges)


__all__ = [
    "ExecutionGraphAnalysis",
    "ExecutionGraphValidationError",
    "ExecutionGraphValidationWork",
    "as_execution_graph_edges",
    "find_execution_graph_cycle",
    "minimum_successful_execution_graph_validation_work",
    "validate_execution_graph",
]
