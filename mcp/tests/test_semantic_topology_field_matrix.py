"""Taxonomy-driven mutation matrix for applicable structural graph facts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
    SubTaskRef,
)
from agents_remember.tasks.document_field_effects import (
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffect,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import SemanticTopologyError
from test_closeout_projection_member_helpers import (
    MASTER,
    PREDECESSOR,
    SUCCESSOR,
    _different,
    _documents,
    _fingerprint,
    _ref,
)


def _with_graph(
    sprint: ResolvedTaskDocument,
    graph: SprintExecutionGraph,
) -> ResolvedTaskDocument:
    return replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": graph}),
    )


def _endpoint_ref(
    endpoint: TaskDocumentRef | SprintExecutionEndpoint,
    *,
    old: TaskDocumentRef,
    new: TaskDocumentRef,
) -> TaskDocumentRef | SprintExecutionEndpoint:
    if isinstance(endpoint, SprintExecutionEndpoint):
        return endpoint.model_copy(update={"ref": new}) if endpoint.ref == old else endpoint
    return new if endpoint == old else endpoint


def _graph_with_ref(
    graph: SprintExecutionGraph,
    *,
    old: TaskDocumentRef,
    new: TaskDocumentRef,
) -> SprintExecutionGraph:
    nodes = [
        node.model_copy(update={"ref": new}) if node.ref == old else node for node in graph.nodes
    ]
    edges = [
        edge.model_copy(
            update={
                "predecessor": _endpoint_ref(edge.predecessor, old=old, new=new),
                "successor": _endpoint_ref(edge.successor, old=old, new=new),
            }
        )
        for edge in graph.edges
    ]
    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def _renumber_candidate_placement(
    graph: SprintExecutionGraph,
    *,
    old: str,
    new: str,
) -> SprintExecutionGraph:
    nodes = [
        node.model_copy(
            update={"leafIds": [new if leaf_id == old else leaf_id for leaf_id in node.leafIds]}
        )
        for node in graph.nodes
    ]
    edges: list[SprintExecutionEdge] = []
    for edge in graph.edges:
        predecessor = edge.predecessor
        successor = edge.successor
        if isinstance(predecessor, SprintExecutionEndpoint) and predecessor.leafId == old:
            predecessor = predecessor.model_copy(update={"leafId": new})
        if isinstance(successor, SprintExecutionEndpoint) and successor.leafId == old:
            successor = successor.model_copy(update={"leafId": new})
        edges.append(edge.model_copy(update={"predecessor": predecessor, "successor": successor}))
    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def test_every_parent_row_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    row = master.document.subTasks[0]
    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[SubTaskRef].items():
        replacement = _different(getattr(row, name))
        changed_graph = graph
        changed_sprint = sprint
        changed_candidate = candidate
        if name == "masterRef":
            replacement = _ref("declared/task.json")
        elif name == "file":
            replacement = "renamed-leaf.md"
            changed_leaf_ref = _ref("master/renamed-leaf.json")
            changed_candidate = replace(
                candidate,
                ref=changed_leaf_ref,
                path=Path(changed_leaf_ref.path),
            )
        elif name == "number":
            replacement = "L01-changed"
            changed_graph = _renumber_candidate_placement(
                graph,
                old=row.number,
                new=cast(str, replacement),
            )
            changed_sprint = _with_graph(sprint, changed_graph)
            changed_candidate = replace(
                candidate,
                document=candidate.document.model_copy(update={"id": replacement}),
            )
        changed_row = row.model_copy(update={name: replacement})
        changed_master = replace(
            master,
            document=master.document.model_copy(
                update={"subTasks": [changed_row, *master.document.subTasks[1:]]}
            ),
        )
        if name == "masterRef":
            with pytest.raises(SemanticTopologyError) as refused:
                _fingerprint(changed_sprint, changed_master, changed_candidate, changed_graph)
            assert refused.value.status == "semantic-topology-parent-row-invalid"
            continue
        changed = _fingerprint(
            changed_sprint,
            changed_master,
            changed_candidate,
            changed_graph,
        )
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name


def test_every_task_document_ref_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)

    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[TaskDocumentRef].items():
        changed_ref = sprint.ref.model_copy(update={name: _different(getattr(sprint.ref, name))})
        changed_sprint = replace(sprint, ref=changed_ref, path=Path(changed_ref.path))
        changed = _fingerprint(changed_sprint, master, candidate, graph)
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name


def test_every_candidate_graph_node_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    node = graph.nodes[1]

    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[SprintExecutionNode].items():
        changed_master = master
        changed_candidate = candidate
        if name == "kind":
            changed_node = node.model_copy(update={"kind": "master", "leafIds": []})
            edges = [
                graph.edges[0].model_copy(update={"successor": MASTER}),
                graph.edges[1].model_copy(update={"predecessor": MASTER}),
            ]
            changed_graph = graph.model_copy(
                update={"nodes": [graph.nodes[0], changed_node, graph.nodes[2]], "edges": edges}
            )
        elif name == "ref":
            changed_ref = _ref("renamed-master/task.json")
            changed_graph = _graph_with_ref(graph, old=MASTER, new=changed_ref)
            changed_master = replace(master, ref=changed_ref, path=Path(changed_ref.path))
            changed_leaf_ref = _ref("renamed-master/leaf.json")
            changed_candidate = replace(
                candidate,
                ref=changed_leaf_ref,
                path=Path(changed_leaf_ref.path),
            )
        else:
            changed_node = node.model_copy(update={"leafIds": [*node.leafIds, "L03"]})
            changed_graph = graph.model_copy(
                update={"nodes": [graph.nodes[0], changed_node, graph.nodes[2]]}
            )
        changed_sprint = _with_graph(sprint, changed_graph)
        changed = _fingerprint(
            changed_sprint,
            changed_master,
            changed_candidate,
            changed_graph,
        )
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name


def test_every_relevant_edge_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    edge = graph.edges[0]

    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[SprintExecutionEdge].items():
        if name == "predecessor":
            replacement: object = SprintExecutionEndpoint(ref=PREDECESSOR)
        elif name == "successor":
            successor = cast(SprintExecutionEndpoint, edge.successor)
            replacement = successor.model_copy(update={"leafId": "L02"})
        else:
            replacement = _different(getattr(edge, name))
        changed_edge = edge.model_copy(update={name: replacement})
        changed_graph = graph.model_copy(update={"edges": [changed_edge, graph.edges[1]]})
        changed_sprint = _with_graph(sprint, changed_graph)
        changed = _fingerprint(changed_sprint, master, candidate, changed_graph)
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name


def test_every_relevant_endpoint_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    edge = graph.edges[0]
    endpoint = cast(SprintExecutionEndpoint, edge.successor)

    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[SprintExecutionEndpoint].items():
        changed_endpoint = (
            endpoint.model_copy(update={"ref": SUCCESSOR, "leafId": None})
            if name == "ref"
            else endpoint.model_copy(update={"leafId": "L02"})
        )
        changed_edge = edge.model_copy(update={"successor": changed_endpoint})
        changed_graph = graph.model_copy(update={"edges": [changed_edge, graph.edges[1]]})
        changed_sprint = _with_graph(sprint, changed_graph)
        changed = _fingerprint(changed_sprint, master, candidate, changed_graph)
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name


def test_every_graph_container_field_uses_its_canonical_taxonomy_effect() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)

    for name, effects in TASK_DOCUMENT_FIELD_EFFECTS[SprintExecutionGraph].items():
        replacement = (
            [graph.nodes[1], graph.nodes[0], graph.nodes[2]]
            if name == "nodes"
            else [graph.edges[0]]
        )
        changed_graph = graph.model_copy(update={name: replacement})
        changed_sprint = _with_graph(sprint, changed_graph)
        changed = _fingerprint(changed_sprint, master, candidate, changed_graph)
        if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
            assert changed != baseline, name
        else:
            assert changed == baseline, name
