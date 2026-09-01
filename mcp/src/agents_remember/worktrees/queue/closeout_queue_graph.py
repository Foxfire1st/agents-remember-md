"""Bounded sprint-graph construction for the closeout queue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agents_remember.models.closeout.projection import (
    MAX_CLOSEOUT_CANDIDATES,
    CloseoutProjectionMember,
)
from agents_remember.models.queue.closeout_queue import (
    MAX_CLOSEOUT_GRAPH_EDGES,
    MAX_CLOSEOUT_MASTERS,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
    derived_leaf_placement,
    leaf_placement_facts,
)
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
)
from agents_remember.tasks.semantic_topology_graph import (
    SemanticTopologyGraphIndex,
    SemanticTopologyGraphIndexError,
    build_semantic_topology_graph_index,
)

from .closeout_queue_errors import CloseoutQueueError, bounded_queue_failure_detail
from .closeout_queue_evidence import PRIORITY_RANK, GradeAuthority, planning_authorities


@dataclass(frozen=True)
class QueueGraphContext:
    """One bounded, immutable scheduling projection of the sprint topology."""

    sprint: ResolvedTaskDocument
    graph: SprintExecutionGraph
    semantic_topology_index: SemanticTopologyGraphIndex
    masters: dict[TaskDocumentRef, ResolvedTaskDocument]
    revision: str
    node_order: dict[SprintExecutionNode, int]
    nodes_by_master: dict[TaskDocumentRef, tuple[SprintExecutionNode, ...]]
    # Leaf document ref -> authored (or derived, L11-R2) segment node; lump masters
    # resolve through ``nodes_by_master`` instead.
    leaf_nodes: dict[TaskDocumentRef, SprintExecutionNode]
    leaf_facts: tuple[dict[str, Any], ...]
    incomplete_predecessors: dict[SprintExecutionNode, tuple[SprintExecutionNode, ...]]
    grade_authority: GradeAuthority


def graph_context(
    topology: TaskDocumentTopology,
    sprint_ref: TaskDocumentRef,
    *,
    authored_graph: SprintExecutionGraph,
    strict_registers: bool = True,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None = None,
) -> QueueGraphContext:
    """Resolve, validate, cap, and index one sprint execution graph.

    ``authored_graph`` is the already-resolved consumer source. The semantic index
    validates this potentially separate object once, then materializes the sole
    deep-immutable graph used by the returned context and its sprint projection.
    ``strict_registers`` guards mutations: a malformed canonical planning register
    refuses with the repair named. Read paths (L13-R4) pass ``False`` so a malformed
    register degrades the projection instead of failing it.
    """

    sprint, graph, master_map = _validated_graph_documents(topology, sprint_ref, overrides)
    try:
        topology_index = build_semantic_topology_graph_index(
            graph,
            _candidate_leaf_ids(master_map),
            authored_graph=authored_graph,
        )
    except SemanticTopologyGraphIndexError as exc:
        raise CloseoutQueueError(exc.status, exc.detail) from exc
    graph = topology_index.boundGraph
    sprint = _sprint_with_bound_graph(sprint, graph)
    completed = {ref for ref, master in master_map.items() if master.document.status == "Completed"}
    leaf_nodes, leaf_facts = _leaf_node_index(graph, master_map, completed)
    try:
        judgments, priorities = planning_authorities(sprint, strict=strict_registers)
    except CloseoutQueueError as exc:
        raise CloseoutQueueError(
            exc.status,
            bounded_queue_failure_detail(
                exc,
                stage="queue-register-planning",
                side="task-document",
                name="planning-registers",
            ),
        ) from exc
    return QueueGraphContext(
        sprint=sprint,
        graph=graph,
        semantic_topology_index=topology_index,
        masters=master_map,
        revision=_graph_revision(graph, master_map),
        node_order={node: index for index, node in enumerate(graph.nodes)},
        nodes_by_master=_nodes_by_master(graph),
        leaf_nodes=leaf_nodes,
        leaf_facts=leaf_facts,
        incomplete_predecessors=incomplete_predecessor_map(graph, completed=completed),
        grade_authority=GradeAuthority(sprint, judgments, priorities),
    )


def _sprint_with_bound_graph(
    sprint: ResolvedTaskDocument,
    graph: SprintExecutionGraph,
) -> ResolvedTaskDocument:
    """Give this read context the already-validated immutable graph instance."""

    document = sprint.document.model_copy(update={"executionGraph": graph})
    return replace(sprint, document=document)


def _validated_graph_documents(
    topology: TaskDocumentTopology,
    sprint_ref: TaskDocumentRef,
    overrides: Mapping[TaskDocumentRef, TaskDocument] | None,
) -> tuple[
    ResolvedTaskDocument,
    SprintExecutionGraph,
    dict[TaskDocumentRef, ResolvedTaskDocument],
]:
    """Resolve and capacity-check one sprint and its authoritative master documents."""

    try:
        sprint = topology.resolve(sprint_ref, overrides)
    except TaskDocumentRefError as exc:
        raise CloseoutQueueError(
            exc.status,
            bounded_queue_failure_detail(
                exc,
                stage="queue-sprint-resolution",
                side="task-document",
                name="sprint",
            ),
        ) from exc
    graph = sprint.document.executionGraph
    if graph is None:
        raise CloseoutQueueError(
            "task-execution-topology-migration-required",
            "sprint has no executionGraph; the sprint runs atomic-sequentially by default "
            "(one source-pair-selected atomic master exposes implementation at a time), "
            "or bootstrap a graph "
            "with task_doc.author_execution_graph",
        )
    if len(graph.nodes) > MAX_CLOSEOUT_MASTERS:
        raise CloseoutQueueError(
            "closeout-queue-master-capacity-exceeded",
            f"sprint has more than {MAX_CLOSEOUT_MASTERS} graph masters; split it before queue admission",
        )
    if len(graph.edges) > MAX_CLOSEOUT_GRAPH_EDGES:
        raise CloseoutQueueError(
            "closeout-queue-edge-capacity-exceeded",
            f"sprint has more than {MAX_CLOSEOUT_GRAPH_EDGES} dependency edges; split it before queue admission",
        )
    try:
        masters = topology.validate_execution_topology(sprint_ref, overrides=overrides)
    except TaskDocumentRefError as exc:
        raise CloseoutQueueError(
            exc.status,
            bounded_queue_failure_detail(
                exc,
                stage="queue-topology-validation",
                side="task-document",
                name="execution-graph",
            ),
        ) from exc
    master_map = {master.ref: master for master in masters}
    if (
        sum(len(master_map[ref].document.subTasks) for ref in graph.master_refs())
        > MAX_CLOSEOUT_CANDIDATES
    ):
        raise CloseoutQueueError(
            "closeout-queue-capacity-exceeded",
            f"sprint has more than {MAX_CLOSEOUT_CANDIDATES} leaf candidates; split it before queue admission",
        )
    return sprint, graph, master_map


def _candidate_leaf_ids(
    masters: Mapping[TaskDocumentRef, ResolvedTaskDocument],
) -> dict[TaskDocumentRef, tuple[str, ...]]:
    """Return every live child row that can become a closeout candidate."""

    return {
        ref: tuple(row.number for row in master.document.subTasks if row.file)
        for ref, master in masters.items()
    }


def _graph_revision(
    graph: SprintExecutionGraph,
    masters: Mapping[TaskDocumentRef, ResolvedTaskDocument],
) -> str:
    payload = {
        "executionGraph": graph.model_dump(mode="json"),
        "executionNatures": [
            {
                "taskDocumentRef": ref.model_dump(mode="json"),
                "executionNature": masters[ref].document.executionNature,
            }
            for ref in graph.master_refs()
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _nodes_by_master(
    graph: SprintExecutionGraph,
) -> dict[TaskDocumentRef, tuple[SprintExecutionNode, ...]]:
    grouped: dict[TaskDocumentRef, list[SprintExecutionNode]] = {}
    for node in graph.nodes:
        grouped.setdefault(node.ref, []).append(node)
    return {ref: tuple(nodes) for ref, nodes in grouped.items()}


def _leaf_node_index(
    graph: SprintExecutionGraph,
    masters: dict[TaskDocumentRef, ResolvedTaskDocument],
    completed: set[TaskDocumentRef],
) -> tuple[dict[TaskDocumentRef, SprintExecutionNode], tuple[dict[str, Any], ...]]:
    """Fold authored and derived (L11-R2) leaf placements into one leaf->node index."""

    leaf_nodes: dict[TaskDocumentRef, SprintExecutionNode] = {}
    facts: list[dict[str, Any]] = []
    for master in masters.values():
        placement = derived_leaf_placement(
            graph,
            master.ref,
            [row.number for row in master.document.subTasks],
            completed,
        )
        targets = {**placement.placed, **placement.derived}
        master_dir = Path(master.ref.path).parent
        for row in master.document.subTasks:
            node = targets.get(row.number)
            if node is None or not row.file:
                continue
            leaf_ref = TaskDocumentRef(
                repository=master.ref.repository,
                path=f"{master_dir}/{Path(row.file).stem}.json",
            )
            leaf_nodes[leaf_ref] = node
        facts.extend(leaf_placement_facts(master.ref.key, placement))
    return leaf_nodes, tuple(facts)


def candidate_node(
    graph: QueueGraphContext, owning_master: TaskDocumentRef, leaf_ref: TaskDocumentRef
) -> SprintExecutionNode | None:
    """The graph node scheduling one candidate: the lump, or its leaf's segment."""
    nodes = graph.nodes_by_master.get(owning_master, ())
    if len(nodes) == 1:
        return nodes[0]
    return graph.leaf_nodes.get(leaf_ref)


def candidate_predecessors(
    graph: QueueGraphContext, owning_master: TaskDocumentRef, leaf_ref: TaskDocumentRef
) -> list[SprintExecutionNode]:
    """Incomplete predecessors of the candidate's own node (L11-R3).

    An edge into a segment blocks exactly that segment's leafs. A candidate whose leaf
    cannot be mapped falls back to the union of its master's nodes -- conservative:
    it may block more, never less.
    """

    node = candidate_node(graph, owning_master, leaf_ref)
    if node is not None:
        return list(graph.incomplete_predecessors[node])
    return list(master_incomplete_predecessors(graph, owning_master))


def predecessor_label(node: SprintExecutionNode) -> str:
    """One predecessor node as a waiting-reason label; segments carry their leaf list."""
    if node.kind == "segment":
        return f"{node.ref.key} (leafs: {', '.join(node.leafIds)})"
    return node.ref.key


def predecessor_waiting_reasons(
    graph: QueueGraphContext, owning_master: TaskDocumentRef, leaf_ref: TaskDocumentRef
) -> list[str]:
    """The candidate's ``predecessor-incomplete:`` waiting reasons, leaf-aware (L11-R3)."""
    return [
        f"predecessor-incomplete: {predecessor_label(node)}"
        for node in candidate_predecessors(graph, owning_master, leaf_ref)
    ]


def ready_sort_key(graph: QueueGraphContext, view: CloseoutProjectionMember) -> tuple[Any, ...]:
    """Priority rank, then the candidate node's declaration order, then leaf identity."""
    node = candidate_node(graph, view.owningMaster, view.taskDocumentRef)
    order = (
        graph.node_order[node]
        if node is not None
        else min(
            (graph.node_order[owned] for owned in graph.nodes_by_master.get(view.owningMaster, ())),
            default=-1,
        )
    )
    return (
        PRIORITY_RANK[view.priority],
        order,
        view.taskDocumentRef.key,
    )


def master_incomplete_predecessors(
    graph: QueueGraphContext, master_ref: TaskDocumentRef
) -> tuple[SprintExecutionNode, ...]:
    """Union of every incomplete predecessor across all of the master's nodes."""
    return tuple(
        dict.fromkeys(
            predecessor
            for node in graph.nodes_by_master.get(master_ref, ())
            for predecessor in graph.incomplete_predecessors[node]
        )
    )


def incomplete_predecessor_map(
    graph: SprintExecutionGraph,
    *,
    completed: set[TaskDocumentRef],
) -> dict[SprintExecutionNode, tuple[SprintExecutionNode, ...]]:
    """Build every node's predecessor set in one bounded O(V+E) pass.

    Completion is master-granular: a node counts complete when its master document is
    Completed. An edge into a segment therefore blocks exactly that segment's leafs
    until the predecessor's master completes (L11-R3).
    """

    incomplete: dict[SprintExecutionNode, list[SprintExecutionNode]] = {
        node: [] for node in graph.nodes
    }
    successors: dict[SprintExecutionNode, list[SprintExecutionNode]] = {
        node: [] for node in graph.nodes
    }
    for edge in graph.edges:
        predecessor = graph.resolve_endpoint(edge.predecessor)
        successors[predecessor].append(graph.resolve_endpoint(edge.successor))
    for predecessor in graph.nodes:
        if predecessor.ref in completed:
            continue
        for successor in successors[predecessor]:
            incomplete[successor].append(predecessor)
    return {node: tuple(predecessors) for node, predecessors in incomplete.items()}
