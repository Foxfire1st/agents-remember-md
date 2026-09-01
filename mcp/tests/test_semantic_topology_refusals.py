"""Exact typed refusals for semantic-topology/v2 derivation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from agents_remember.tasks import semantic_topology as topology_module
from agents_remember.tasks.document import (
    HeaderNote,
    SprintExecutionGraph,
    TaskDocument,
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
from agents_remember.worktrees import task_leaf_binding
from agents_remember.worktrees.queue import closeout_projection_members as members
from pydantic import Field
from test_closeout_projection_member_helpers import (
    _bound_sprint,
    _documents,
    _queue_graph,
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


@pytest.mark.parametrize(
    ("mutation", "status", "detail"),
    [
        (
            "split",
            "semantic-topology-parent-binding-split",
            "row number and row source identify different candidate leaves",
        ),
        (
            "stem-only",
            "semantic-topology-parent-binding-stem-only",
            "candidate source or file stem matches without the candidate document ID",
        ),
        (
            "wrong-directory",
            "semantic-topology-parent-binding-wrong-directory",
            "candidate leaf is not a direct child of its declared parent directory",
        ),
        (
            "source-mismatch",
            "semantic-topology-parent-binding-source-mismatch",
            "the candidate-ID row points at a different canonical child source",
        ),
        (
            "invalid-source",
            "semantic-topology-parent-row-invalid",
            "leaf parent row must name one direct Markdown child source",
        ),
    ],
)
def test_semantic_topology_refuses_every_composite_parent_binding_near_miss(
    mutation: str,
    status: str,
    detail: str,
) -> None:
    sprint, master, candidate = _documents()
    row = master.document.subTasks[0]
    other = master.document.subTasks[1]
    if mutation == "split":
        rows = [
            row.model_copy(update={"file": "other.md"}),
            other.model_copy(update={"file": "leaf.md"}),
        ]
    elif mutation == "stem-only":
        rows = [other.model_copy(update={"file": "leaf.md"})]
    elif mutation == "source-mismatch":
        rows = [row.model_copy(update={"file": "other.md"})]
    elif mutation == "invalid-source":
        rows = [row.model_copy(update={"file": "nested/leaf.md"})]
    else:
        rows = list(master.document.subTasks)
        changed_ref = _ref("other/leaf.json")
        candidate = replace(candidate, ref=changed_ref, path=Path(changed_ref.path))
    master = replace(
        master,
        document=master.document.model_copy(update={"subTasks": rows}),
    )
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    index = _index(graph, master)
    assert index is not None
    sprint = _bound_sprint(sprint, index)

    with pytest.raises(SemanticTopologyError) as caught:
        semantic_topology_projection(
            sprint,
            master,
            candidate,
            graph_index=index,
        )
    assert caught.value.status == status
    assert caught.value.detail == detail


def test_semantic_topology_and_lifecycle_binding_share_one_authority() -> None:
    assert (
        topology_module.require_canonical_leaf_binding
        is task_leaf_binding.require_canonical_leaf_binding
    )


def test_semantic_topology_refuses_unclassified_nested_schema_and_unsupported_version() -> None:
    class FutureHeaderNote(HeaderNote):
        futureTopologyFact: str = "unclassified"

    class FutureTaskDocument(TaskDocument):
        headerNotes: list[FutureHeaderNote] = Field(default_factory=list)

    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    index = _index(graph, master)
    assert index is not None
    sprint = _bound_sprint(sprint, index)
    future = FutureTaskDocument.model_validate(candidate.document.model_dump(by_alias=True))
    with pytest.raises(SemanticTopologyError) as unclassified:
        semantic_topology_projection(
            sprint,
            master,
            replace(candidate, document=future),
            graph_index=index,
        )
    assert unclassified.value.status == "semantic-topology-schema-unclassified"
    assert "futureTopologyFact" in unclassified.value.detail

    with pytest.raises(SemanticTopologyError) as unsupported:
        semantic_topology_projection(
            sprint,
            master,
            candidate,
            graph_index=index,
            schema_version="semantic-topology/v1",
        )
    assert unsupported.value.status == "semantic-topology-schema-unsupported"
    assert unsupported.value.detail == (
        "unsupported semantic topology schema 'semantic-topology/v1'"
    )


def test_queue_adapter_preserves_exact_typed_refusal_status_and_detail() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    context = _queue_graph(sprint, master, candidate, graph)
    with pytest.raises(members.CloseoutQueueError) as caught:
        members.semantic_topology_projection(
            context.sprint,
            master,
            candidate,
            graph=context,
            schema_version="semantic-topology/v1",
        )
    assert caught.value.status == "semantic-topology-schema-unsupported"
    assert str(caught.value) == (
        "semantic-topology-schema-unsupported: unsupported semantic topology schema "
        "'semantic-topology/v1'"
    )
