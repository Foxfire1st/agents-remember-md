"""Focused proof for candidate-local closeout readiness helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
)
from agents_remember.tasks.document_field_effects import (
    TASK_DOCUMENT_FIELD_EFFECTS,
    TaskDocumentFieldEffect,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import (
    SEMANTIC_TOPOLOGY_SCHEMA,
    semantic_topology_fingerprint,
    semantic_topology_projection,
)
from agents_remember.tasks.semantic_topology_graph import (
    SemanticTopologyGraphIndex,
    build_semantic_topology_graph_index,
)
from agents_remember.worktrees.queue import closeout_projection_members as members
from agents_remember.worktrees.queue.closeout_queue_graph import QueueGraphContext
from pydantic import BaseModel


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def _ref(path: str) -> TaskDocumentRef:
    return TaskDocumentRef(repository="agents-remember", path=path)


def test_admission_and_activation_waiting_reasons_are_explicit() -> None:
    door = _value(
        admissionProvenance=_value(
            resourceReady=False,
            resourceReason="worker unavailable",
            admissionReady=False,
            admissionReason="predecessor active",
        )
    )
    assert members._admission_waiting_reasons(door) == [
        "resource-unavailable: worker unavailable",
        "admission-blocked: predecessor active",
    ]

    # Activation is an independent waiting input. Dependency ordering does not
    # invent a first-master owner when the sprint has no graph.
    context = _value(graph=None)
    assert members._dependency_waiting_and_order(context, 7) == ([], 7)


def test_dependency_order_falls_back_or_uses_the_exact_graph_node() -> None:
    context = _value(
        graph=None,
        master=_value(ref=_ref("sprint/master.json")),
        candidate=_value(ref=_ref("sprint/leaf.json")),
    )
    assert members._dependency_waiting_and_order(context, 12) == ([], 12)

    graph = _value(node_order={"candidate": 4})
    context.graph = graph
    with (
        mock.patch.object(
            members, "predecessor_waiting_reasons", return_value=["predecessor-active"]
        ),
        mock.patch.object(members, "candidate_node", side_effect=(None, "candidate")),
    ):
        assert members._dependency_waiting_and_order(context, 12) == (
            ["predecessor-active"],
            12,
        )
        assert members._dependency_waiting_and_order(context, 12) == (
            ["predecessor-active"],
            4012,
        )


SPRINT = _ref("sprint/task.json")
PREDECESSOR = _ref("predecessor/task.json")
MASTER = _ref("master/task.json")
SUCCESSOR = _ref("successor/task.json")
LEAF = _ref("master/leaf.json")
NOW = "2026-08-31T00:00:00+00:00"


def _graph(*, leaf_ids: list[str] | None = None) -> SprintExecutionGraph:
    candidate_leaf_ids = leaf_ids or ["L01", "L02"]
    return SprintExecutionGraph(
        nodes=[
            SprintExecutionNode(ref=PREDECESSOR),
            SprintExecutionNode(kind="segment", ref=MASTER, leafIds=candidate_leaf_ids),
            SprintExecutionNode(ref=SUCCESSOR),
        ],
        edges=[
            SprintExecutionEdge(
                predecessor=PREDECESSOR,
                successor=SprintExecutionEndpoint(ref=MASTER, leafId="L01"),
                reason="Predecessor supplies the candidate.",
                judgmentId="J-1",
            ),
            SprintExecutionEdge(
                predecessor=SprintExecutionEndpoint(ref=MASTER, leafId="L02"),
                successor=SUCCESSOR,
                reason="Candidate supplies the successor.",
                judgmentId="J-2",
            ),
        ],
    )


def _candidate_document() -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": "L01",
            "slug": "leaf",
            "title": "Stable topology",
            "kind": "subTask",
            "status": "inProgress",
            "statusNote": "Building",
            "repo": "agents-remember",
            "type": "Implementation",
            "createdAt": NOW,
            "headerNotes": [{"label": "Owner", "value": "worker"}],
            "enclosures": [{"leafId": "L01", "enclosurePath": "/tmp/contract.md"}],
            "executionRegistrations": [
                {
                    "sourceKind": "terminal-catalog-seat",
                    "role": "worker",
                    "sourceId": "seat-1",
                    "observedAt": NOW,
                }
            ],
            "lifecycleId": "lifecycle-1",
            "objective": "Replace the topology identity.",
            "requirements": ["R01"],
            "design": "Typed projection.",
            "steps": [
                {
                    "id": "S1",
                    "title": "Build",
                    "outcome": "Built",
                    "status": "done",
                    "substeps": [
                        {
                            "id": "S1.1",
                            "title": "Proof",
                            "status": "done",
                            "disposition": {
                                "reason": "Covered elsewhere.",
                                "recordedAt": NOW,
                                "lifecycleId": "lifecycle-1",
                            },
                        }
                    ],
                    "disposition": {
                        "reason": "Covered elsewhere.",
                        "recordedAt": NOW,
                        "lifecycleId": "lifecycle-1",
                    },
                }
            ],
            "codeExamples": [
                {
                    "id": "E1",
                    "title": "Projection",
                    "distinctChange": "Typed",
                    "why": "Stable",
                    "language": "python",
                    "snippet": "projection()",
                }
            ],
            "decisions": [{"at": NOW, "decision": "Use v2", "rationale": "Stable"}],
            "routeReview": {
                "candidateTree": "a" * 40,
                "verdict": "pass",
                "verdictRef": "notes/review.md",
                "reviewedAt": NOW,
                "routes": [
                    {
                        "route": "topology",
                        "verdict": "pass",
                        "evidenceRef": "notes/review.md",
                    }
                ],
            },
            "openQuestions": ["None"],
            "references": ["requirements/R01.md"],
            "sections": [{"heading": "Notes", "body": "Audit prose."}],
        }
    )


def _documents(
    graph: SprintExecutionGraph | None = None,
) -> tuple[ResolvedTaskDocument, ResolvedTaskDocument, ResolvedTaskDocument]:
    execution_graph = graph or _graph()
    sprint_doc = TaskDocument.model_validate(
        {
            "id": "SPRINT",
            "slug": "sprint",
            "title": "Sprint",
            "kind": "master",
            "status": "inProgress",
            "repo": "agents-remember",
            "createdAt": NOW,
            "orchestrates": ["predecessor", "master", "successor"],
            "subTasks": [
                {"number": "M1", "name": "Predecessor", "masterRef": PREDECESSOR},
                {"number": "M2", "name": "Master", "masterRef": MASTER},
                {"number": "M3", "name": "Successor", "masterRef": SUCCESSOR},
            ],
            "executionGraph": execution_graph,
            "seats": [{"role": "architect", "label": "Planning", "state": "active"}],
        }
    )
    master_doc = TaskDocument.model_validate(
        {
            "id": "MASTER",
            "slug": "master",
            "title": "Master",
            "kind": "master",
            "status": "inProgress",
            "repo": "agents-remember",
            "createdAt": NOW,
            "executionNature": "organizational",
            "subTasks": [
                {
                    "number": "L01",
                    "name": "Stable topology",
                    "file": "leaf.md",
                    "status": "inProgress",
                    "scope": "Semantic topology",
                },
                {
                    "number": "L02",
                    "name": "Other leaf",
                    "file": "other.md",
                    "status": "planning",
                },
            ],
            "discardedSubTasks": [
                {
                    "number": "L00",
                    "name": "Discarded leaf",
                    "file": "discarded.md",
                    "scope": "Historical audit",
                    "reason": "Never started.",
                    "discardedAt": NOW,
                    "proof": {
                        "taskDocumentRef": {
                            "repository": "agents-remember",
                            "path": "master/discarded.json",
                        },
                        "taskState": "planning-unstarted",
                        "enclosureState": "absent",
                        "locatorState": "absent",
                        "doorState": "absent",
                        "operationState": "absent",
                        "seatState": "absent",
                        "reviewState": "absent",
                        "commitState": "absent",
                        "childJson": {"state": "missing"},
                        "childMarkdown": {"state": "missing"},
                        "fingerprint": "b" * 64,
                    },
                }
            ],
        }
    )
    return (
        ResolvedTaskDocument(ref=SPRINT, path=Path(SPRINT.path), document=sprint_doc),
        ResolvedTaskDocument(ref=MASTER, path=Path(MASTER.path), document=master_doc),
        ResolvedTaskDocument(ref=LEAF, path=Path(LEAF.path), document=_candidate_document()),
    )


def _semantic_index(
    graph: SprintExecutionGraph,
    master: ResolvedTaskDocument,
) -> SemanticTopologyGraphIndex:
    candidate_leaf_ids = {
        master.ref: tuple(row.number for row in master.document.subTasks if row.file)
    }
    return build_semantic_topology_graph_index(
        graph,
        candidate_leaf_ids,
        authored_graph=graph,
    )


def _bound_sprint(
    sprint: ResolvedTaskDocument,
    index: SemanticTopologyGraphIndex,
) -> ResolvedTaskDocument:
    return replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": index.boundGraph}),
    )


def _queue_graph(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    graph: SprintExecutionGraph,
) -> QueueGraphContext:
    semantic_index = _semantic_index(graph, master)
    graph = semantic_index.boundGraph
    sprint = _bound_sprint(sprint, semantic_index)
    node_order = {node: index for index, node in enumerate(graph.nodes)}
    nodes_by_master = {
        ref: tuple(node for node in graph.nodes if node.ref == ref)
        for ref in {node.ref for node in graph.nodes}
    }
    candidate_node = next(node for node in graph.nodes if "L01" in node.leafIds)
    return QueueGraphContext(
        sprint=sprint,
        graph=graph,
        semantic_topology_index=semantic_index,
        masters={MASTER: master},
        revision="a" * 64,
        node_order=node_order,
        nodes_by_master=nodes_by_master,
        leaf_nodes={candidate.ref: candidate_node},
        leaf_facts=(),
        incomplete_predecessors={node: () for node in graph.nodes},
        grade_authority=cast(Any, None),
    )


def _fingerprint(
    sprint: ResolvedTaskDocument,
    master: ResolvedTaskDocument,
    candidate: ResolvedTaskDocument,
    graph: SprintExecutionGraph | None,
) -> str:
    graph_index = _semantic_index(graph, master) if graph is not None else None
    if graph_index is not None:
        sprint = _bound_sprint(sprint, graph_index)
    return semantic_topology_fingerprint(
        sprint,
        master,
        candidate,
        graph_index=graph_index,
    )


def _different(value: object) -> object:
    if value is None:
        changed: object = "changed"
    elif isinstance(value, str):
        changed = f"{value}-changed"
    elif isinstance(value, list):
        changed = [*value, "changed"]
    elif isinstance(value, tuple):
        changed = (*value, "changed")
    elif isinstance(value, bool):
        changed = not value
    elif isinstance(value, int):
        changed = value + 1
    elif isinstance(value, BaseModel):
        name = next(iter(type(value).model_fields))
        changed = value.model_copy(update={name: _different(getattr(value, name))})
    else:
        changed = "changed"
    return changed


def _field_paths(
    model: BaseModel,
    prefix: tuple[str | int, ...] = (),
    inherited_effects: frozenset[TaskDocumentFieldEffect] | None = None,
) -> list[
    tuple[
        tuple[str | int, ...],
        type[BaseModel],
        str,
        frozenset[TaskDocumentFieldEffect],
    ]
]:
    paths: list[
        tuple[
            tuple[str | int, ...],
            type[BaseModel],
            str,
            frozenset[TaskDocumentFieldEffect],
        ]
    ] = []
    for name in type(model).model_fields:
        path = (*prefix, name)
        effects = TASK_DOCUMENT_FIELD_EFFECTS[type(model)][name]
        effective = effects if inherited_effects is None else inherited_effects & effects
        paths.append((path, type(model), name, effective))
        value = getattr(model, name)
        if isinstance(value, BaseModel):
            paths.extend(_field_paths(value, path, effective))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, BaseModel):
                    paths.extend(_field_paths(item, (*path, index), effective))
    return paths


def _replace_path(model: BaseModel, path: tuple[str | int, ...]) -> BaseModel:
    field = cast(str, path[0])
    value = getattr(model, field)
    if len(path) == 1:
        replacement = _different(value)
    elif isinstance(path[1], int):
        items = list(cast(list[object], value))
        index = path[1]
        item = cast(BaseModel, items[index])
        items[index] = _replace_path(item, path[2:])
        replacement = items
    else:
        replacement = _replace_path(cast(BaseModel, value), path[1:])
    return model.model_copy(update={field: replacement})


def test_semantic_topology_exact_v2_shape_and_canonical_ordering() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    index = _semantic_index(graph, master)
    projection = semantic_topology_projection(
        _bound_sprint(sprint, index),
        master,
        candidate,
        graph_index=index,
    )
    value = cast(dict[str, Any], projection.canonical_value())
    placement = cast(dict[str, Any], value["placement"])
    node = cast(dict[str, Any], placement["node"])
    edges = cast(list[dict[str, Any]], placement["relevantEdges"])

    assert value["schema"] == SEMANTIC_TOPOLOGY_SCHEMA
    assert value["masterExecutionNature"] == "organizational"
    assert placement["mode"] == "dag"
    assert placement["nodeOrder"] == 1
    assert node["leafIds"] == ["L01", "L02"]
    assert len(edges) == 2
    assert "reason" not in str(edges)
    assert "judgmentId" not in str(edges)

    reversed_edges = graph.model_copy(update={"edges": list(reversed(graph.edges))})
    reversed_sprint = replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": reversed_edges}),
    )
    baseline = _fingerprint(sprint, master, candidate, graph)
    assert baseline == "643f6709f510f1ae381e681a5a39bf89f42585e639975d75246749b4502ceda0"
    assert baseline == _fingerprint(reversed_sprint, master, candidate, reversed_edges)


def test_every_present_nonstructural_current_and_nested_field_is_excluded() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    for owner in ("sprint", "master", "candidate"):
        resolved = {"sprint": sprint, "master": master, "candidate": candidate}[owner]
        for path, _, _, effects in _field_paths(resolved.document):
            if TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY in effects:
                continue
            changed_document = cast(TaskDocument, _replace_path(resolved.document, path))
            changed_sprint = (
                replace(sprint, document=changed_document) if owner == "sprint" else sprint
            )
            changed_master = (
                replace(master, document=changed_document) if owner == "master" else master
            )
            changed_candidate = (
                replace(candidate, document=changed_document) if owner == "candidate" else candidate
            )
            changed_graph = cast(SprintExecutionGraph, changed_sprint.document.executionGraph)
            assert (
                _fingerprint(
                    changed_sprint,
                    changed_master,
                    changed_candidate,
                    changed_graph,
                )
                == baseline
            ), (owner, path)


def test_graph_node_order_leaf_placement_and_relevant_endpoints_change_identity() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)

    reordered = graph.model_copy(update={"nodes": [graph.nodes[1], graph.nodes[0], graph.nodes[2]]})
    reordered_sprint = replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": reordered}),
    )
    assert _fingerprint(reordered_sprint, master, candidate, reordered) != baseline

    changed_leafs = _graph(leaf_ids=["L02", "L01", "L03"])
    changed_leafs_sprint = replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": changed_leafs}),
    )
    assert _fingerprint(changed_leafs_sprint, master, candidate, changed_leafs) != baseline

    endpoint = cast(SprintExecutionEndpoint, graph.edges[0].successor)
    changed_edge = graph.edges[0].model_copy(
        update={"successor": endpoint.model_copy(update={"leafId": "L02"})}
    )
    changed_edges = graph.model_copy(update={"edges": [changed_edge, graph.edges[1]]})
    changed_edges_sprint = replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": changed_edges}),
    )
    assert _fingerprint(changed_edges_sprint, master, candidate, changed_edges) != baseline


def test_ref_repository_and_path_components_change_identity() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    baseline = _fingerprint(sprint, master, candidate, graph)
    for changed_ref in (
        TaskDocumentRef(repository="other-repo", path=SPRINT.path),
        TaskDocumentRef(repository=SPRINT.repository, path="other-sprint/task.json"),
    ):
        changed_sprint = replace(sprint, ref=changed_ref, path=Path(changed_ref.path))
        assert _fingerprint(changed_sprint, master, candidate, graph) != baseline


def test_atomic_sequential_is_an_explicit_v2_variant_with_effective_atomic_nature() -> None:
    sprint, master, candidate = _documents()
    graphless_sprint = replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": None}),
    )
    projection = semantic_topology_projection(
        graphless_sprint,
        master,
        candidate,
        graph_index=None,
    )
    value = projection.canonical_value()
    assert value["masterExecutionNature"] == "atomic"
    assert value["placement"] == {"mode": "atomic-sequential"}
    assert (
        _fingerprint(graphless_sprint, master, candidate, None)
        == "e7c441a4e3b2f295ff24d0af282da910903dc93ad31a7cf38a14e39c843ec8c7"
    )
