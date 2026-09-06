"""Exact typed refusals for semantic-topology/v2 derivation."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from agents_remember.tasks.document import (
    SprintExecutionGraph,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import (
    SemanticTopologyError,
    semantic_topology_projection,
)
from agents_remember.tasks.semantic_topology_graph import (
    SemanticTopologyGraphIndex,
    SemanticTopologyGraphIndexError,
)
from test_closeout_projection_member_helpers import (
    _bound_sprint,
    _documents,
    _ref,
    _semantic_index,
)


def _index(
    graph: SprintExecutionGraph | None,
    master: ResolvedTaskDocument,
) -> SemanticTopologyGraphIndex | None:
    return _semantic_index(graph, master) if graph is not None else None


def _bound_context_sprint(
    sprint: ResolvedTaskDocument,
    supplied: SprintExecutionGraph | None,
    index: SemanticTopologyGraphIndex | None,
) -> ResolvedTaskDocument:
    if index is None or sprint.document.executionGraph is not supplied:
        return sprint
    return _bound_sprint(sprint, index)


@pytest.mark.parametrize(
    ("mutation", "status", "detail"),
    [
        (
            "missing-row",
            "semantic-topology-parent-row-missing",
            "parent task contains no composite row binding for leaf 'L01'",
        ),
        (
            "duplicate-row",
            "semantic-topology-parent-row-ambiguous",
            "candidate leaf resolves to multiple parent row identities",
        ),
        (
            "blank-row",
            "semantic-topology-parent-binding-stem-only",
            "candidate source or file stem matches without the candidate document ID",
        ),
        (
            "missing-execution-nature",
            "semantic-topology-execution-nature-missing",
            "an authored execution graph requires the master's declared execution nature",
        ),
        (
            "missing-dag-context",
            "semantic-topology-dag-placement-missing",
            "an authored execution graph requires exact candidate placement",
        ),
        (
            "missing-dag-node",
            "semantic-topology-dag-placement-missing",
            "candidate has no exact node in the authored execution graph",
        ),
        (
            "graph-context-mismatch",
            "semantic-topology-graph-context-mismatch",
            "the supplied graph differs from the sprint's authored graph",
        ),
        (
            "invalid-edge",
            "semantic-topology-graph-invalid",
            "an authored graph edge has invalid or ambiguous endpoints",
        ),
    ],
)
def test_semantic_topology_refuses_exact_missing_ambiguous_and_malformed_facts(
    mutation: str,
    status: str,
    detail: str,
) -> None:
    sprint, master, candidate = _documents()
    authored = cast(SprintExecutionGraph, sprint.document.executionGraph)
    supplied: SprintExecutionGraph | None = authored
    if mutation == "missing-row":
        master = replace(master, document=master.document.model_copy(update={"subTasks": []}))
    elif mutation == "duplicate-row":
        master = replace(
            master,
            document=master.document.model_copy(
                update={"subTasks": [master.document.subTasks[0]] * 2}
            ),
        )
    elif mutation == "blank-row":
        row = master.document.subTasks[0].model_copy(update={"number": " "})
        master = replace(
            master,
            document=master.document.model_copy(update={"subTasks": [row]}),
        )
    elif mutation == "missing-execution-nature":
        master = replace(
            master,
            document=master.document.model_copy(update={"executionNature": None}),
        )
    elif mutation == "missing-dag-context":
        supplied = None
    elif mutation == "missing-dag-node":
        supplied = authored.model_copy(update={"nodes": [authored.nodes[0], authored.nodes[2]]})
        sprint = replace(
            sprint,
            document=sprint.document.model_copy(update={"executionGraph": supplied}),
        )
    elif mutation == "graph-context-mismatch":
        supplied = authored.model_copy(update={"edges": []})
    elif mutation == "invalid-edge":
        invalid_edge = authored.edges[0].model_copy(
            update={"predecessor": _ref("missing/task.json")}
        )
        supplied = authored.model_copy(update={"edges": [invalid_edge, *authored.edges[1:]]})
        sprint = replace(
            sprint,
            document=sprint.document.model_copy(update={"executionGraph": supplied}),
        )

    index = _index(supplied, master)
    sprint = _bound_context_sprint(sprint, supplied, index)
    with pytest.raises(SemanticTopologyError) as caught:
        semantic_topology_projection(
            sprint,
            master,
            candidate,
            graph_index=index,
        )
    assert caught.value.status == status
    assert caught.value.detail == detail


def test_semantic_topology_refuses_duplicate_node_during_whole_graph_admission() -> None:
    sprint, master, _ = _documents()
    authored = cast(SprintExecutionGraph, sprint.document.executionGraph)
    duplicate = authored.model_copy(update={"nodes": [*authored.nodes, authored.nodes[1]]})

    with pytest.raises(SemanticTopologyGraphIndexError) as invalid_graph:
        _index(duplicate, master)
    assert invalid_graph.value.status == "semantic-topology-graph-invalid"
    assert invalid_graph.value.detail == (
        "the validated authored graph could not form an immutable context"
    )
