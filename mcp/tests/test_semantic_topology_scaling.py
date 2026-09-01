"""Measured work-unit regressions for indexed semantic topology projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

from agents_remember.application.task_docs.task_execution_topology import (
    _MUTATION_HANDLERS,
    _ExecutionGraphAuthoring,
    _GraphDraft,
)
from agents_remember.tasks import semantic_topology_graph as graph_module
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionEndpoint,
    SprintExecutionGraph,
    SprintExecutionNode,
    SubTaskRef,
    derived_leaf_placement,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.semantic_topology import semantic_topology_fingerprint_with_work
from agents_remember.tasks.semantic_topology_graph import (
    MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK,
    SemanticTopologyGraphIndex,
    SemanticTopologyGraphIndexError,
    build_semantic_topology_graph_index,
)
from agents_remember.worktrees.queue.closeout_projection_members import (
    candidate_task_topology_fingerprint,
)
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from pydantic import ValidationError
from test_closeout_projection_member_helpers import (
    MASTER,
    _documents,
    _queue_graph,
    _ref,
)

SIZES = (8, 16, 32)


def _bound_sprint(
    sprint: ResolvedTaskDocument,
    index: SemanticTopologyGraphIndex,
) -> ResolvedTaskDocument:
    return replace(
        sprint,
        document=sprint.document.model_copy(update={"executionGraph": index.boundGraph}),
    )


def _population(
    size: int,
    *,
    dense: bool,
) -> tuple[
    ResolvedTaskDocument,
    ResolvedTaskDocument,
    tuple[ResolvedTaskDocument, ...],
    SprintExecutionGraph,
]:
    sprint, master, prototype = _documents()
    leaf_ids = [f"L{index:03d}" for index in range(size)]
    rows = [
        SubTaskRef(
            number=leaf_id,
            name=f"Leaf {index}",
            file=f"leaf-{index:03d}.md",
            status="inProgress",
        )
        for index, leaf_id in enumerate(leaf_ids)
    ]
    nodes = [
        SprintExecutionNode(kind="segment", ref=MASTER, leafIds=[leaf_id]) for leaf_id in leaf_ids
    ]
    edge_pairs = (
        [(left, right) for left in range(size) for right in range(left + 1, size)]
        if dense
        else [(index, index + 1) for index in range(size - 1)]
    )
    edges = [
        SprintExecutionEdge(
            predecessor=SprintExecutionEndpoint(ref=MASTER, leafId=leaf_ids[left]),
            successor=SprintExecutionEndpoint(ref=MASTER, leafId=leaf_ids[right]),
            reason="Declared scheduling dependency.",
        )
        for left, right in edge_pairs
    ]
    graph = SprintExecutionGraph(nodes=nodes, edges=edges)
    sprint = replace(
        sprint,
        document=sprint.document.model_copy(
            update={
                "subTasks": [SubTaskRef(number="M1", name="Master", masterRef=MASTER)],
                "executionGraph": graph,
            }
        ),
    )
    master = replace(
        master,
        document=master.document.model_copy(update={"subTasks": rows}),
    )
    candidates = tuple(
        ResolvedTaskDocument(
            ref=_ref(f"master/leaf-{index:03d}.json"),
            path=Path(f"master/leaf-{index:03d}.json"),
            document=prototype.document.model_copy(update={"id": leaf_id}),
        )
        for index, leaf_id in enumerate(leaf_ids)
    )
    return sprint, master, candidates, graph


def _population_work(size: int, *, dense: bool) -> tuple[int, int, int]:
    sprint, master, candidates, graph = _population(size, dense=dense)
    candidate_leaf_ids = {MASTER: tuple(row.number for row in master.document.subTasks)}
    index = build_semantic_topology_graph_index(
        graph,
        candidate_leaf_ids,
        authored_graph=graph,
    )
    sprint = _bound_sprint(sprint, index)
    fingerprints: set[str] = set()
    candidate_work = 0
    for candidate in candidates:
        fingerprint, work = semantic_topology_fingerprint_with_work(
            sprint,
            master,
            candidate,
            graph_index=index,
        )
        assert work is not None
        assert work.placementLookups == 2
        assert work.matchedNodeReads == 1
        fingerprints.add(fingerprint)
        candidate_work += work.total
    edge_count = len(graph.edges)
    assert len(fingerprints) == size
    assert index.buildWork.total == 3 * size + 6 * edge_count
    assert candidate_work == 3 * size + 2 * edge_count
    assert index.populationWork.total == candidate_work
    assert index.totalWork == (index.buildWork.total + candidate_work + index.validationWork.total)
    return index.totalWork, edge_count, index.validationWork.total


def test_broad_and_dense_populations_have_exact_noncomposed_work_bounds() -> None:
    broad = [_population_work(size, dense=False) for size in SIZES]
    dense = [_population_work(size, dense=True) for size in SIZES]

    for size, (work, edge_count, validation_work) in zip(SIZES, broad, strict=True):
        assert edge_count == size - 1
        assert work == 6 * size + 8 * edge_count + validation_work
    for size, (work, edge_count, validation_work) in zip(SIZES, dense, strict=True):
        assert edge_count == size * (size - 1) // 2
        assert work == 6 * size + 8 * edge_count + validation_work

    assert all(work <= MAX_SEMANTIC_TOPOLOGY_GRAPH_WORK for work, _, _ in [*broad, *dense])


def test_separate_graph_resolutions_are_bound_once_before_population_reads() -> None:
    sprint, master, candidates, graph = _population(32, dense=True)
    candidate_leaf_ids = {MASTER: tuple(row.number for row in master.document.subTasks)}
    separately_resolved = SprintExecutionGraph.model_validate(graph.model_dump(mode="json"))
    original_canonicalizer = graph_module._canonical_graph_bytes
    original_admission = cast(
        Callable[[SprintExecutionGraph], SprintExecutionGraph],
        SprintExecutionGraph._check_graph_shape,
    )
    canonicalizations = 0
    admissions = 0

    def _traced_canonicalizer(value: SprintExecutionGraph) -> bytes:
        nonlocal canonicalizations
        canonicalizations += 1
        return original_canonicalizer(value)

    def _traced_admission(value: SprintExecutionGraph) -> SprintExecutionGraph:
        nonlocal admissions
        admissions += 1
        return original_admission(value)

    graph_module._canonical_graph_bytes = _traced_canonicalizer
    SprintExecutionGraph._check_graph_shape = _traced_admission  # pyright: ignore[reportAttributeAccessIssue]
    try:
        index = build_semantic_topology_graph_index(
            separately_resolved,
            candidate_leaf_ids,
            authored_graph=graph,
        )
        assert canonicalizations == 2
        sprint = _bound_sprint(sprint, index)
        for candidate in candidates:
            _, work = semantic_topology_fingerprint_with_work(
                sprint,
                master,
                candidate,
                graph_index=index,
            )
            assert work is not None
        assert canonicalizations == 2
    finally:
        graph_module._canonical_graph_bytes = original_canonicalizer
        SprintExecutionGraph._check_graph_shape = original_admission  # pyright: ignore[reportAttributeAccessIssue]

    assert not index.is_bound_to(graph)
    assert index.is_bound_to(index.boundGraph)
    assert index.validationWork.graphCanonicalizations == 2
    assert index.validationWork.graphSnapshots == 1
    assert index.validationWork.graphAdmissions == 1
    assert index.validationWork.graphComparisons == 1
    assert admissions == 1
    assert index.validationWork.canonicalByteBlocks > 0
    assert index.buildWork.nodeProjections == len(graph.nodes)
    assert index.buildWork.edgeProjections == len(graph.edges)


def test_snapshot_then_valid_source_mutation_cannot_change_index_generation() -> None:
    sprint, master, candidate = _documents()
    graph = cast(SprintExecutionGraph, sprint.document.executionGraph)
    candidate_leaf_ids = {MASTER: tuple(row.number for row in master.document.subTasks)}
    captured_graph = graph.model_dump(mode="json")
    original_snapshot = graph_module.immutable_semantic_topology_graph
    mutations = 0

    def _snapshot_then_mutate(canonical_graph: bytes) -> SprintExecutionGraph:
        nonlocal mutations
        bound = original_snapshot(canonical_graph)
        endpoint = cast(SprintExecutionEndpoint, graph.edges[1].predecessor)
        graph.edges[1] = graph.edges[1].model_copy(
            update={"predecessor": endpoint.model_copy(update={"leafId": "L01"})}
        )
        SprintExecutionGraph.model_validate(graph.model_dump(mode="json"))
        mutations += 1
        return bound

    graph_module.immutable_semantic_topology_graph = _snapshot_then_mutate
    try:
        index = build_semantic_topology_graph_index(
            graph,
            candidate_leaf_ids,
            authored_graph=graph,
        )
    finally:
        graph_module.immutable_semantic_topology_graph = original_snapshot

    bound_sprint = _bound_sprint(sprint, index)
    observed, _ = semantic_topology_fingerprint_with_work(
        bound_sprint,
        master,
        candidate,
        graph_index=index,
    )
    fresh_index = build_semantic_topology_graph_index(
        graph,
        candidate_leaf_ids,
        authored_graph=graph,
    )
    fresh_sprint = _bound_sprint(sprint, fresh_index)
    changed, _ = semantic_topology_fingerprint_with_work(
        fresh_sprint,
        master,
        candidate,
        graph_index=fresh_index,
    )

    assert mutations == 1
    assert index.is_bound_to(index.boundGraph)
    assert index.boundGraph.model_dump(mode="json") == captured_graph
    assert graph.model_dump(mode="json") != captured_graph
    assert observed == "643f6709f510f1ae381e681a5a39bf89f42585e639975d75246749b4502ceda0"
    assert changed == "ff3812ff9dc31626918a66d2cbb315aaca4dda07168040cdf5ec6d6f622b2c9f"
    assert observed != changed


def test_whole_graph_admission_refuses_duplicate_edges_and_cycles_before_fingerprint() -> None:
    _, duplicate_master, _, duplicate_source = _population(3, dense=False)
    duplicate = duplicate_source.model_copy(
        update={"edges": [*duplicate_source.edges, duplicate_source.edges[0].model_copy()]}
    )
    _, cycle_master, _, cycle_source = _population(3, dense=False)
    cycle_edge = SprintExecutionEdge(
        predecessor=SprintExecutionEndpoint(ref=MASTER, leafId="L002"),
        successor=SprintExecutionEndpoint(ref=MASTER, leafId="L000"),
        reason="Invalid cycle back to the first segment.",
    )
    cycle = SprintExecutionGraph.model_construct(
        nodes=cycle_source.nodes,
        edges=[*cycle_source.edges, cycle_edge],
    )

    for invalid, master in ((duplicate, duplicate_master), (cycle, cycle_master)):
        try:
            SprintExecutionGraph.model_validate(invalid.model_dump(mode="json"))
        except ValidationError:
            pass
        else:
            raise AssertionError("the canonical graph validator must refuse the malformed graph")
        try:
            build_semantic_topology_graph_index(
                invalid,
                {MASTER: tuple(row.number for row in master.document.subTasks)},
                authored_graph=invalid,
            )
        except SemanticTopologyGraphIndexError as exc:
            assert exc.status == "semantic-topology-graph-invalid"
            assert exc.detail == "the validated authored graph could not form an immutable context"
        else:
            raise AssertionError("whole-graph invalidity must refuse before fingerprinting")


def test_bound_graph_is_recursively_immutable_and_serialization_preserving() -> None:
    _, master, _, graph = _population(4, dense=False)
    index = build_semantic_topology_graph_index(
        graph,
        {MASTER: tuple(row.number for row in master.document.subTasks)},
        authored_graph=graph,
    )
    bound = index.boundGraph

    assert bound.model_dump(mode="json") == graph.model_dump(mode="json")
    assert isinstance(bound.nodes, tuple)
    assert isinstance(bound.nodes[0].leafIds, tuple)
    assert isinstance(bound.edges, tuple)
    endpoint = cast(SprintExecutionEndpoint, bound.edges[0].predecessor)
    for mutation in (
        lambda: setattr(bound, "edges", ()),
        lambda: setattr(bound.nodes[0], "leafIds", ("changed",)),
        lambda: setattr(bound.edges[0], "predecessor", bound.edges[0].successor),
        lambda: setattr(endpoint, "leafId", "changed"),
        lambda: bound.edges.append(bound.edges[0]),
        lambda: bound.nodes[0].leafIds.append("changed"),
    ):
        try:
            mutation()
        except (AttributeError, TypeError, ValidationError):
            continue
        raise AssertionError("the bound graph accepted an in-place structural mutation")

    shallow_update = bound.model_copy(update={"edges": []})
    assert shallow_update is not bound
    assert not index.is_bound_to(shallow_update)
    assert index.is_bound_to(bound)


def test_queue_adapter_refuses_mutated_source_and_never_returns_stale_bound_bytes() -> None:
    sprint, master, candidate = _documents()
    authored = cast(SprintExecutionGraph, sprint.document.executionGraph)
    context = _queue_graph(sprint, master, candidate, authored)
    assert context.graph is context.semantic_topology_index.boundGraph
    assert context.sprint.document.executionGraph is context.graph
    before = candidate_task_topology_fingerprint(
        context.sprint,
        master,
        candidate,
        graph=context,
    )

    replacement = context.graph.model_copy()
    replaced_sprint = replace(
        context.sprint,
        document=context.sprint.document.model_copy(update={"executionGraph": replacement}),
    )
    try:
        candidate_task_topology_fingerprint(
            replaced_sprint,
            master,
            candidate,
            graph=context,
        )
    except CloseoutQueueError as exc:
        assert exc.status == "semantic-topology-graph-context-mismatch"
    else:
        raise AssertionError("a graph-bearing sprint replacement must refuse")

    try:
        context.graph.edges[1] = context.graph.edges[1]
    except TypeError:
        pass
    else:
        raise AssertionError("the context-owned graph collection must be immutable")

    endpoint = cast(SprintExecutionEndpoint, authored.edges[1].predecessor)
    authored.edges[1] = authored.edges[1].model_copy(
        update={"predecessor": endpoint.model_copy(update={"leafId": "L01"})}
    )
    SprintExecutionGraph.model_validate(authored.model_dump(mode="json"))
    try:
        candidate_task_topology_fingerprint(
            sprint,
            master,
            candidate,
            graph=context,
        )
    except CloseoutQueueError as exc:
        assert exc.status == "semantic-topology-graph-context-mismatch"
    else:
        raise AssertionError("a mutated source graph must not reuse the old bound context")

    fresh = _queue_graph(sprint, master, candidate, authored)
    expected = candidate_task_topology_fingerprint(
        fresh.sprint,
        master,
        candidate,
        graph=fresh,
    )
    assert before == "643f6709f510f1ae381e681a5a39bf89f42585e639975d75246749b4502ceda0"
    assert expected == "ff3812ff9dc31626918a66d2cbb315aaca4dda07168040cdf5ec6d6f622b2c9f"
    assert expected != before


def test_all_task_doc_graph_authoring_mutations_retain_mutable_validated_drafts() -> None:
    before = _ref("before/task.json")
    temporary = _ref("temporary/task.json")
    graph = SprintExecutionGraph(
        nodes=[
            SprintExecutionNode(kind="segment", ref=MASTER, leafIds=["L01", "L02"]),
            SprintExecutionNode(kind="segment", ref=MASTER, leafIds=["L03"]),
            SprintExecutionNode(ref=before),
        ]
    )
    index = build_semantic_topology_graph_index(
        graph,
        {MASTER: ("L01", "L02", "L03")},
        authored_graph=graph,
    )
    assert isinstance(graph.nodes, list)
    assert isinstance(graph.nodes[0].leafIds, list)
    assert index.boundGraph is not graph

    authoring = _ExecutionGraphAuthoring.model_validate(
        {
            "mutations": [
                {"op": "add_node", "ref": temporary.model_dump(mode="json")},
                {
                    "op": "add_edge",
                    "predecessor": before.model_dump(mode="json"),
                    "successor": temporary.model_dump(mode="json"),
                    "reason": "Temporary authoring edge.",
                    "judgmentId": "J-1",
                },
                {
                    "op": "remove_edge",
                    "predecessor": before.model_dump(mode="json"),
                    "successor": temporary.model_dump(mode="json"),
                    "judgmentId": "J-1",
                },
                {
                    "op": "move_leaf",
                    "ref": MASTER.model_dump(mode="json"),
                    "leafId": "L02",
                    "toSegment": "L03",
                    "judgmentId": "J-2",
                },
                {"op": "remove_node", "ref": temporary.model_dump(mode="json")},
                {
                    "op": "set_nature",
                    "ref": MASTER.model_dump(mode="json"),
                    "executionNature": "organizational",
                    "judgmentId": "J-3",
                },
            ]
        }
    )
    assert set(_MUTATION_HANDLERS) == {
        "add_node",
        "remove_node",
        "add_edge",
        "remove_edge",
        "move_leaf",
        "set_nature",
    }
    draft = _GraphDraft(nodes=list(graph.nodes), edges=list(graph.edges), natures={})
    commanded = {MASTER, before, temporary}
    for mutation in authoring.mutations:
        _MUTATION_HANDLERS[mutation.op](draft, mutation, commanded)
    candidate = SprintExecutionGraph(nodes=draft.nodes, edges=draft.edges)

    assert draft.natures == {MASTER: "organizational"}
    assert [node.leafIds for node in candidate.nodes if node.ref == MASTER] == [
        ["L01"],
        ["L03", "L02"],
    ]
    assert candidate.edges == []
    assert SprintExecutionGraph.model_validate(candidate.model_dump(mode="json")).model_dump(
        mode="json"
    ) == candidate.model_dump(mode="json")


def test_one_time_graph_binding_refuses_a_mismatched_resolution() -> None:
    _, master, _, graph = _population(8, dense=False)
    candidate_leaf_ids = {MASTER: tuple(row.number for row in master.document.subTasks)}
    mismatched = graph.model_copy(update={"edges": []})

    try:
        build_semantic_topology_graph_index(
            graph,
            candidate_leaf_ids,
            authored_graph=mismatched,
        )
    except SemanticTopologyGraphIndexError as exc:
        assert exc.status == "semantic-topology-graph-context-mismatch"
        assert exc.detail == "the supplied graph differs from the sprint's authored graph"
    else:
        raise AssertionError("a mismatched authored graph must refuse before candidate projection")


def test_one_time_graph_validation_bytes_are_inside_the_enforced_work_budget() -> None:
    leaf_ids = [
        "L000",
        "X" * (3 * 1024 * 1024),
        *(f"UNKNOWN-{index}" for index in range(15_498)),
    ]
    graph = SprintExecutionGraph(
        nodes=[SprintExecutionNode(kind="segment", ref=MASTER, leafIds=leaf_ids)],
    )

    try:
        build_semantic_topology_graph_index(
            graph,
            {MASTER: ("L000",)},
            authored_graph=graph,
        )
    except SemanticTopologyGraphIndexError as exc:
        assert exc.status == "semantic-topology-graph-work-budget-exceeded"
        assert "leaf placements=15500" in exc.detail
        assert "one-time graph validation work=" in exc.detail
    else:
        raise AssertionError("canonical graph bytes outside the work budget must refuse")


def test_shared_lump_and_multi_leaf_segment_account_for_candidate_incident_reads() -> None:
    _, master, candidates, _ = _population(4, dense=False)
    candidate_leaf_ids = {MASTER: tuple(row.number for row in master.document.subTasks)}
    before = _ref("before/task.json")
    after = _ref("after/task.json")
    leaf_ids = list(candidate_leaf_ids[MASTER])

    for candidate_node in (
        SprintExecutionNode(ref=MASTER),
        SprintExecutionNode(kind="segment", ref=MASTER, leafIds=leaf_ids),
    ):
        graph = SprintExecutionGraph(
            nodes=[SprintExecutionNode(ref=before), candidate_node, SprintExecutionNode(ref=after)],
            edges=[
                SprintExecutionEdge(
                    predecessor=before,
                    successor=MASTER,
                    reason="Candidate input.",
                ),
                SprintExecutionEdge(
                    predecessor=MASTER,
                    successor=after,
                    reason="Candidate output.",
                ),
            ],
        )
        index = build_semantic_topology_graph_index(
            graph,
            candidate_leaf_ids,
            authored_graph=graph,
        )
        observed = sum(
            index.candidate_slice(MASTER, candidate.document.id).work.total
            for candidate in candidates
        )

        assert observed == 20
        assert index.populationWork.candidateCount == 4
        assert index.populationWork.matchedNodeReads == 4
        assert index.populationWork.incidentEdgeReads == 8
        assert index.populationWork.total == observed


def test_unknown_leaf_drift_is_counted_and_only_the_explicit_work_budget_refuses() -> None:
    graph = SprintExecutionGraph(
        nodes=[SprintExecutionNode(kind="segment", ref=MASTER, leafIds=["L000", "UNKNOWN"])],
    )
    index = build_semantic_topology_graph_index(
        graph,
        {MASTER: ("L000",)},
        authored_graph=graph,
    )
    placement = derived_leaf_placement(graph, MASTER, ["L000"], set())

    assert placement.unknown_leaf_ids == ("UNKNOWN",)
    assert index.buildWork.leafPlacements == 2
    assert index.candidate_slice(MASTER, "L000").work.total == 3

    excessive_leaf_ids = ["L000", *(f"UNKNOWN-{index}" for index in range(34_304))]
    excessive = SprintExecutionGraph(
        nodes=[SprintExecutionNode(kind="segment", ref=MASTER, leafIds=excessive_leaf_ids)],
    )
    try:
        build_semantic_topology_graph_index(
            excessive,
            {MASTER: ("L000",)},
            authored_graph=excessive,
        )
    except SemanticTopologyGraphIndexError as exc:
        assert exc.status == "semantic-topology-graph-work-budget-exceeded"
        assert "leaf placements=34305" in exc.detail
        assert "candidates=1" in exc.detail
    else:
        raise AssertionError("an over-budget unknown-leaf population must refuse")
