"""Fail-closed compiler for complete reverse memory dependency closure."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from typing import NoReturn, Protocol

from agents_remember.models.task_intent import TaskIntentIdentity

from .errors import ScopeFailure, ScopeUnprovenError
from .models import (
    CheckerScopePolicy,
    DependencySnapshot,
    EdgeClass,
    EdgeClassEvidence,
    ScopeCandidateIdentity,
    ScopeEdge,
    ScopeManifest,
    ScopeNode,
    SourceIndexObservation,
    canonical_digest,
)
from .registry import (
    EDGE_OWNER_CONTRACTS,
    checker_registry_version,
    checker_scope_registry,
)


class ScopeAuthority(Protocol):
    """Existing owners re-observed around dependency acquisition."""

    def observe(self) -> ScopeCandidateIdentity: ...


class DependencySnapshotAuthority(Protocol):
    """Canonical dependency owners observed for one exact candidate."""

    def observe(self, candidate: ScopeCandidateIdentity) -> DependencySnapshot: ...


def dependency_edge(
    source: str,
    target: str,
    edge_class: EdgeClass,
    reasons: tuple[str, ...],
) -> ScopeEdge:
    """Build one self-verifying, canonically addressed owner edge."""

    contract = EDGE_OWNER_CONTRACTS[edge_class]
    payload = {
        "source": source,
        "target": target,
        "edgeClass": edge_class,
        "authorityNamespace": contract.authority_namespace,
        "extractorVersion": contract.extractor_version,
        "validatorVersion": contract.validator_version,
        "reasons": list(reasons),
    }
    return ScopeEdge(contentDigest=canonical_digest(payload), **payload)


def build_dependency_snapshot(
    *,
    candidate: ScopeCandidateIdentity,
    source_index: SourceIndexObservation,
    nodes: Iterable[ScopeNode],
    edges: Iterable[ScopeEdge],
) -> DependencySnapshot:
    """Freeze one already owner-observed graph with exact class evidence."""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: item.nodeId))
    ordered_edges = tuple(
        sorted(edges, key=lambda item: (item.source, item.target, item.edgeClass))
    )
    evidence = tuple(
        _edge_evidence(edge_class, ordered_edges) for edge_class in EDGE_OWNER_CONTRACTS
    )
    payload = {
        "schema": "memory-dependency-snapshot/v1",
        "candidateDigest": candidate.digest,
        "sourceIndex": source_index.model_dump(mode="json"),
        "nodes": [node.model_dump(mode="json") for node in ordered_nodes],
        "edges": [edge.model_dump(mode="json") for edge in ordered_edges],
        "edgeEvidence": [one.model_dump(mode="json") for one in evidence],
    }
    return DependencySnapshot(
        candidateDigest=candidate.digest,
        sourceIndex=source_index,
        nodes=ordered_nodes,
        edges=ordered_edges,
        edgeEvidence=evidence,
        snapshotDigest=canonical_digest(payload),
    )


def compile_scope_manifest(
    authority: ScopeAuthority,
    dependencies: DependencySnapshotAuthority,
    *,
    checkers: Iterable[str],
) -> ScopeManifest:
    """Compile exact reverse closure; never accept a caller filename or full fallback."""

    candidate = authority.observe()
    snapshot = dependencies.observe(candidate)
    policies = _selected_policies(checkers)
    _validate_candidate(candidate)
    _validate_snapshot(candidate, snapshot)
    nodes = {node.nodeId: node for node in snapshot.nodes}
    task_nodes = _task_nodes(candidate)
    for node in task_nodes:
        if node.nodeId in nodes and nodes[node.nodeId] != node:
            _refuse(
                "task-node-private-authority",
                "snapshot replaced a canonical task node",
                node=node.nodeId,
            )
        nodes[node.nodeId] = node
    roots = _changed_roots(candidate)
    _validate_roots(roots, nodes)
    selected_ids, selected_edges = _reverse_closure(roots, nodes, snapshot.edges)
    current = authority.observe()
    if current != candidate:
        _refuse(
            "candidate-moved-during-compilation",
            "code, memory, task, or pair authority changed during scope compilation",
            candidate=candidate.digest,
        )
    selected_nodes = tuple(nodes[node_id] for node_id in sorted(selected_ids))
    selected_edges = tuple(
        sorted(selected_edges, key=lambda edge: (edge.source, edge.target, edge.edgeClass))
    )
    full_only = tuple(sorted(policy.checker for policy in policies if policy.mode == "full-only"))
    manifest_payload = {
        "schema": "memory-dependency-scope/v1",
        "candidateDigest": candidate.digest,
        "checkerRegistryVersion": checker_registry_version(),
        "sourceIndex": snapshot.sourceIndex.model_dump(mode="json"),
        "changedRoots": list(roots),
        "selectedNodes": [node.model_dump(mode="json") for node in selected_nodes],
        "selectedEdges": [edge.model_dump(mode="json") for edge in selected_edges],
        "checkerPolicies": [policy.model_dump(mode="json") for policy in policies],
        "fullOnlyCheckers": list(full_only),
        "incrementalReady": not full_only,
    }
    return ScopeManifest(
        candidateDigest=candidate.digest,
        checkerRegistryVersion=checker_registry_version(),
        sourceIndex=snapshot.sourceIndex,
        changedRoots=roots,
        selectedNodes=selected_nodes,
        selectedEdges=selected_edges,
        checkerPolicies=policies,
        fullOnlyCheckers=full_only,
        incrementalReady=not full_only,
        manifestDigest=canonical_digest(manifest_payload),
    )


def _selected_policies(checkers: Iterable[str]) -> tuple[CheckerScopePolicy, ...]:
    requested = tuple(sorted(set(checkers)))
    if not requested:
        _refuse("checker-population-empty", "an empty checker selection cannot certify scope")
    policies = {policy.checker: policy for policy in checker_scope_registry()}
    if unknown := sorted(set(requested) - set(policies)):
        _refuse("checker-unknown", f"unknown memory checker(s): {unknown}", checker=unknown[0])
    return tuple(policies[name] for name in requested)


def _validate_candidate(candidate: ScopeCandidateIdentity) -> None:
    task = candidate.task
    for side, observation in (("base", task.base), ("candidate", task.candidate)):
        if observation is None:
            _refuse(f"task-{side}-unavailable", f"canonical {side} task observation is missing")
        if observation.semanticTopologyDigest is None:
            _refuse(
                f"task-{side}-topology-unavailable",
                f"canonical R01 {side} topology identity is unavailable",
            )
        if not isinstance(observation.taskIntent, TaskIntentIdentity):
            _refuse(
                f"task-{side}-intent-unavailable",
                f"canonical R02 {side} task intent is unavailable",
            )
    assert task.base is not None and task.candidate is not None
    if task.base.taskRoot != task.candidate.taskRoot:
        _refuse("task-root-ambiguous", "base and candidate task observations use different roots")
    if task.candidate.taskRoot != candidate.pairIdentity.contractPath.rsplit("/enclosures/", 1)[0]:
        _refuse("task-root-pair-mismatch", "task observation root differs from contract task root")


def _validate_snapshot(candidate: ScopeCandidateIdentity, snapshot: DependencySnapshot) -> None:
    if snapshot.candidateDigest != candidate.digest:
        _refuse(
            "dependency-snapshot-candidate-mismatch",
            "dependency snapshot belongs to another candidate",
            snapshot=snapshot.snapshotDigest,
            candidate=candidate.digest,
        )
    index = snapshot.sourceIndex
    if (
        index.candidateDigest != candidate.digest
        or index.codeRoot != candidate.code.root
        or index.memoryRoot != candidate.memory.root
    ):
        _refuse(
            "source-index-candidate-mismatch",
            "citation source index is not bound to the exact candidate roots",
            snapshot=index.snapshotId,
        )
    _require_sorted_unique(snapshot.nodes, key=lambda item: item.nodeId, label="nodes")
    _require_sorted_unique(
        snapshot.edges,
        key=lambda item: (item.source, item.target, item.edgeClass),
        label="edges",
    )
    _validate_edges(snapshot)
    payload = snapshot.model_dump(mode="json", by_alias=True, exclude={"snapshotDigest"})
    if canonical_digest(payload) != snapshot.snapshotDigest:
        _refuse(
            "dependency-snapshot-digest-invalid", "dependency snapshot bytes do not self-verify"
        )


def _validate_edges(snapshot: DependencySnapshot) -> None:
    evidence = {one.edgeClass: one for one in snapshot.edgeEvidence}
    if set(evidence) != set(EDGE_OWNER_CONTRACTS) or len(evidence) != len(snapshot.edgeEvidence):
        _refuse("edge-class-incomplete", "dependency snapshot lacks one exact edge-class proof")
    counts = Counter(edge.edgeClass for edge in snapshot.edges)
    for edge_class, contract in EDGE_OWNER_CONTRACTS.items():
        observed = evidence[edge_class]
        class_edges = tuple(edge for edge in snapshot.edges if edge.edgeClass == edge_class)
        expected_digest = canonical_digest([edge.model_dump(mode="json") for edge in class_edges])
        if (
            observed.authorityNamespace != contract.authority_namespace
            or observed.extractorVersion != contract.extractor_version
            or observed.validatorVersion != contract.validator_version
            or observed.observedEdgeCount != counts[edge_class]
            or observed.evidenceDigest != expected_digest
        ):
            _refuse(
                "edge-class-owner-invalid",
                "edge-class evidence differs from its canonical owner contract",
                edge_class=edge_class,
                owner=observed.authorityNamespace,
            )
        for edge in class_edges:
            payload = edge.model_dump(mode="json", exclude={"contentDigest"})
            if (
                edge.authorityNamespace != contract.authority_namespace
                or edge.extractorVersion != contract.extractor_version
                or edge.validatorVersion != contract.validator_version
                or canonical_digest(payload) != edge.contentDigest
            ):
                _refuse(
                    "dependency-edge-invalid",
                    "dependency edge is fabricated, stale, or from the wrong owner",
                    edge_class=edge_class,
                    owner=edge.authorityNamespace,
                )


def _task_nodes(candidate: ScopeCandidateIdentity) -> tuple[ScopeNode, ...]:
    assert candidate.task.candidate is not None
    observation = candidate.task.candidate
    assert observation.semanticTopologyDigest is not None
    assert isinstance(observation.taskIntent, TaskIntentIdentity)
    return (
        ScopeNode(
            nodeId="task:normative-intent",
            contentDigest=observation.taskIntent.digest,
            authorityNamespace="agents-remember.task-intent",
            validatorVersion=observation.taskIntent.schema_,
            reasons=("canonical R02 candidate observation",),
        ),
        ScopeNode(
            nodeId="task:semantic-topology",
            contentDigest=observation.semanticTopologyDigest,
            authorityNamespace="agents-remember.semantic-topology",
            validatorVersion=observation.semanticTopologySchema,
            reasons=("canonical R01 candidate observation",),
        ),
    )


def _changed_roots(candidate: ScopeCandidateIdentity) -> tuple[str, ...]:
    roots: set[str] = set()
    for delta in (candidate.code, candidate.memory):
        for change in delta.changes:
            for path in (change.oldPath, change.newPath):
                if path is not None:
                    roots.add(f"{delta.namespace}:{path}")
    base = candidate.task.base
    current = candidate.task.candidate
    assert base is not None and current is not None
    if base.semanticTopologyDigest != current.semanticTopologyDigest:
        roots.add("task:semantic-topology")
    if base.taskIntent != current.taskIntent:
        roots.add("task:normative-intent")
    return tuple(sorted(roots))


def _validate_roots(roots: tuple[str, ...], nodes: dict[str, ScopeNode]) -> None:
    for root in roots:
        node = nodes.get(root)
        if node is None:
            _refuse("changed-root-unclassified", "changed root has no owner node", node=root)
        expected = (
            "agents-remember.git-code-tree"
            if root.startswith("code:")
            else "agents-remember.git-memory-tree"
            if root.startswith("memory:")
            else node.authorityNamespace
        )
        if node.authorityNamespace != expected:
            _refuse(
                "changed-root-private-authority",
                "changed root was not emitted by its canonical authority",
                node=root,
                owner=node.authorityNamespace,
            )


def _reverse_closure(
    roots: tuple[str, ...],
    nodes: dict[str, ScopeNode],
    edges: tuple[ScopeEdge, ...],
) -> tuple[set[str], set[ScopeEdge]]:
    outgoing: dict[str, list[ScopeEdge]] = defaultdict(list)
    for edge in edges:
        if edge.source not in nodes or edge.target not in nodes:
            _refuse(
                "dependency-edge-endpoint-missing",
                "dependency edge refers to an absent node",
                node=edge.source if edge.source not in nodes else edge.target,
                edge_class=edge.edgeClass,
            )
        outgoing[edge.source].append(edge)
    selected = set(roots)
    selected_edges: set[ScopeEdge] = set()
    pending = deque(roots)
    while pending:
        source = pending.popleft()
        for edge in outgoing.get(source, ()):
            selected_edges.add(edge)
            if edge.target not in selected:
                selected.add(edge.target)
                pending.append(edge.target)
    return selected, selected_edges


def _edge_evidence(
    edge_class: EdgeClass,
    edges: tuple[ScopeEdge, ...],
) -> EdgeClassEvidence:
    contract = EDGE_OWNER_CONTRACTS[edge_class]
    class_edges = tuple(edge for edge in edges if edge.edgeClass == edge_class)
    return EdgeClassEvidence(
        edgeClass=edge_class,
        authorityNamespace=contract.authority_namespace,
        extractorVersion=contract.extractor_version,
        validatorVersion=contract.validator_version,
        observedEdgeCount=len(class_edges),
        evidenceDigest=canonical_digest([edge.model_dump(mode="json") for edge in class_edges]),
    )


def _require_sorted_unique(values: tuple, *, key, label: str) -> None:
    keys = tuple(key(item) for item in values)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        _refuse(f"dependency-{label}-noncanonical", f"dependency {label} are not sorted and unique")


def _refuse(
    code: str,
    detail: str,
    **evidence: str | None,
) -> NoReturn:
    raise ScopeUnprovenError(
        ScopeFailure(
            code=code,
            detail=detail,
            checker=evidence.get("checker"),
            node=evidence.get("node"),
            edge_class=evidence.get("edge_class"),
            snapshot=evidence.get("snapshot"),
            candidate=evidence.get("candidate"),
            owner=evidence.get("owner"),
        )
    )


__all__ = [
    "DependencySnapshotAuthority",
    "ScopeAuthority",
    "compile_scope_manifest",
]
