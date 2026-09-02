"""Focused closure and fail-closed tests for the R06 scope compiler."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest
from agents_remember.memory_quality.check import AVAILABLE_CHECKS, DRIFT_CHECK_NAME
from agents_remember.memory_quality.incremental_scope.candidate import ContractScopeAuthority
from agents_remember.memory_quality.incremental_scope.compiler import (
    build_dependency_snapshot,
    compile_scope_manifest,
    dependency_edge,
)
from agents_remember.memory_quality.incremental_scope.errors import ScopeUnprovenError
from agents_remember.memory_quality.incremental_scope.models import (
    CanonicalTaskObservation,
    DependencySnapshot,
    GitPathChange,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeNode,
    SourceIndexObservation,
    TaskObservationPair,
)
from agents_remember.memory_quality.incremental_scope.registry import checker_scope_registry
from agents_remember.memory_quality.style.citations.range_resolution import CHECK_NAME
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    TaskIntentIdentity,
    TaskIntentState,
    missing_task_intent,
)


@dataclass
class StaticAuthority:
    candidate: ScopeCandidateIdentity
    replacement: ScopeCandidateIdentity | None = None
    calls: int = 0

    def observe(self) -> ScopeCandidateIdentity:
        self.calls += 1
        if self.replacement is not None and self.calls > 1:
            return self.replacement
        return self.candidate


@dataclass(frozen=True)
class StaticDependencies:
    snapshot: DependencySnapshot

    def observe(self, candidate: ScopeCandidateIdentity) -> DependencySnapshot:
        del candidate
        return self.snapshot


def _task_observation(
    *,
    root: str = "/coord/tasks/repo/master",
    source: str = "a",
    topology: str | None = "b",
    intent: TaskIntentState | None = None,
) -> CanonicalTaskObservation:
    if intent is None:
        intent = TaskIntentIdentity(digest="c" * 64)
    return CanonicalTaskObservation(
        taskRoot=root,
        taskDocumentRef=TaskDocumentRef(repository="repo", path="master/leaf.json"),
        sourceDigest=source * 64,
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="fixture/v1",
        semanticTopologyDigest=None if topology is None else topology * 64,
        taskIntent=intent,
    )


def _candidate(*, task: TaskObservationPair | None = None) -> ScopeCandidateIdentity:
    pair = MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coord/tasks/repo/master/enclosures/leaf/series-contract.md",
        contractDigest="d" * 64,
        codeRoot="/work/code",
        memoryRoot="/work/memory",
        codeSourceBranch="main",
        codeWorkBranch="work",
        codeBaseCommit="1" * 40,
        memorySourceBranch="main-memory",
        memoryWorkBranch="work-memory",
        memoryBaseCommit="2" * 40,
        onboardingRoot="/work/memory/onboarding",
        ledgerPath="/work/memory/memory.md",
    )
    observation = _task_observation()
    return ScopeCandidateIdentity(
        pairIdentity=pair,
        code=GitTreeDelta(
            namespace="code",
            root=pair.codeRoot,
            baseTree="3" * 40,
            candidateTree="4" * 40,
            changes=(
                GitPathChange(
                    status="modified",
                    oldPath="src/a.py",
                    newPath="src/a.py",
                    oldBlob="5" * 40,
                    newBlob="6" * 40,
                ),
            ),
        ),
        memory=GitTreeDelta(
            namespace="memory",
            root=pair.memoryRoot,
            baseTree="7" * 40,
            candidateTree="7" * 40,
            changes=(),
        ),
        task=task or TaskObservationPair(base=observation, candidate=observation),
    )


def _node(node_id: str, authority: str = "agents-remember.memory-document") -> ScopeNode:
    return ScopeNode(
        nodeId=node_id,
        contentDigest=("8" if node_id.startswith("code:") else "9") * 64,
        authorityNamespace=(
            "agents-remember.git-code-tree" if node_id.startswith("code:") else authority
        ),
        validatorVersion="fixture/v1",
        reasons=("owner fixture",),
    )


def _snapshot(candidate: ScopeCandidateIdentity):
    nodes = (
        _node("code:src/a.py"),
        _node("memory:onboarding/src/a.py.md"),
        _node("memory:onboarding/src/overview.md"),
        _node("memory:onboarding/src/overview.index.json"),
        _node("memory:onboarding/claims.md"),
        _node("memory:onboarding/entities.md"),
    )
    edges = (
        dependency_edge(
            "code:src/a.py",
            "memory:onboarding/src/a.py.md",
            "source-to-file-sidecar",
            ("sidecar",),
        ),
        dependency_edge(
            "code:src/a.py",
            "memory:onboarding/src/overview.md",
            "source-to-governing-route",
            ("route",),
        ),
        dependency_edge(
            "memory:onboarding/src/a.py.md",
            "memory:onboarding/claims.md",
            "source-to-citing-memory-document",
            ("citation",),
        ),
        dependency_edge(
            "code:src/a.py",
            "memory:onboarding/entities.md",
            "source-to-entity-manifestation",
            ("entity",),
        ),
        dependency_edge(
            "memory:onboarding/src/overview.md",
            "memory:onboarding/src/overview.index.json",
            "route-index-dependency",
            ("index",),
        ),
    )
    source_index = SourceIndexObservation(
        snapshotId="a" * 64,
        codeRoot=candidate.code.root,
        memoryRoot=candidate.memory.root,
        candidateDigest=candidate.digest,
    )
    return build_dependency_snapshot(
        candidate=candidate,
        source_index=source_index,
        nodes=nodes,
        edges=edges,
    )


def _reason(error: pytest.ExceptionInfo[ScopeUnprovenError]) -> str:
    return error.value.failure.code


def test_direct_transitive_and_reverse_only_dependencies_are_complete() -> None:
    candidate = _candidate()
    manifest = compile_scope_manifest(
        StaticAuthority(candidate),
        StaticDependencies(_snapshot(candidate)),
        checkers=(CHECK_NAME,),
    )

    assert manifest.incrementalReady is True
    assert manifest.changedRoots == ("code:src/a.py",)
    assert {node.nodeId for node in manifest.selectedNodes} == {
        "code:src/a.py",
        "memory:onboarding/claims.md",
        "memory:onboarding/entities.md",
        "memory:onboarding/src/a.py.md",
        "memory:onboarding/src/overview.index.json",
        "memory:onboarding/src/overview.md",
    }
    assert len(manifest.selectedEdges) == 5


def test_manifest_digest_is_stable_across_repeated_owner_observation() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)

    first = compile_scope_manifest(
        StaticAuthority(candidate), StaticDependencies(snapshot), checkers=(CHECK_NAME,)
    )
    second = compile_scope_manifest(
        StaticAuthority(candidate), StaticDependencies(snapshot), checkers=(CHECK_NAME,)
    )

    assert first == second
    assert first.manifestDigest == second.manifestDigest


def test_every_current_checker_has_one_executable_or_full_only_policy() -> None:
    policies = checker_scope_registry()

    assert {policy.checker for policy in policies} == set(AVAILABLE_CHECKS)
    assert [policy.checker for policy in policies if policy.mode == "incremental"] == [CHECK_NAME]
    assert len([policy for policy in policies if policy.mode == "full-only"]) == 6


def test_full_only_checker_remains_pending_without_silent_full_fallback() -> None:
    candidate = _candidate()
    manifest = compile_scope_manifest(
        StaticAuthority(candidate),
        StaticDependencies(_snapshot(candidate)),
        checkers=(DRIFT_CHECK_NAME,),
    )

    assert manifest.incrementalReady is False
    assert manifest.fullOnlyCheckers == (DRIFT_CHECK_NAME,)
    assert "fallback" not in manifest.model_dump(mode="json")


@pytest.mark.parametrize("checkers", [(), ("unknown.check",)])
def test_empty_or_unknown_checker_population_is_scope_unproven(checkers: tuple[str, ...]) -> None:
    candidate = _candidate()
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(_snapshot(candidate)),
            checkers=checkers,
        )

    assert _reason(caught) in {"checker-population-empty", "checker-unknown"}


def test_missing_edge_class_and_hidden_edge_count_are_scope_unproven() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    missing = snapshot.model_copy(update={"edgeEvidence": snapshot.edgeEvidence[:-1]})
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(missing), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "edge-class-incomplete"

    first = snapshot.edgeEvidence[0]
    evidence = (
        first.model_copy(update={"observedEdgeCount": first.observedEdgeCount + 1}),
        *snapshot.edgeEvidence[1:],
    )
    hidden = snapshot.model_copy(update={"edgeEvidence": evidence})
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(hidden), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "edge-class-owner-invalid"


def test_stale_index_and_adjacent_candidate_snapshot_are_scope_unproven() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    adjacent = candidate.model_copy(
        update={"code": candidate.code.model_copy(update={"candidateTree": "f" * 40})}
    )
    stale = snapshot.model_copy(
        update={
            "sourceIndex": snapshot.sourceIndex.model_copy(
                update={"candidateDigest": adjacent.digest}
            )
        }
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(stale), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "source-index-candidate-mismatch"

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(adjacent), StaticDependencies(snapshot), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "dependency-snapshot-candidate-mismatch"


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (TaskObservationPair(base=None, candidate=_task_observation()), "task-base-unavailable"),
        (
            TaskObservationPair(
                base=_task_observation(topology=None), candidate=_task_observation()
            ),
            "task-base-topology-unavailable",
        ),
        (
            TaskObservationPair(
                base=_task_observation(intent=missing_task_intent()),
                candidate=_task_observation(),
            ),
            "task-base-intent-unavailable",
        ),
        (
            TaskObservationPair(
                base=_task_observation(),
                candidate=_task_observation(root="/coord/tasks/other/master"),
            ),
            "task-root-ambiguous",
        ),
    ],
)
def test_missing_or_ambiguous_r01_r02_authority_is_scope_unproven(
    task: TaskObservationPair,
    expected: str,
) -> None:
    candidate = _candidate(task=task)
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(_snapshot(candidate)),
            checkers=(CHECK_NAME,),
        )
    assert _reason(caught) == expected


def test_private_root_or_task_identity_and_moved_candidate_are_refused() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    nodes = list(snapshot.nodes)
    nodes[0] = nodes[0].model_copy(update={"authorityNamespace": "caller.private"})
    private = build_dependency_snapshot(
        candidate=candidate,
        source_index=snapshot.sourceIndex,
        nodes=nodes,
        edges=snapshot.edges,
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(private), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "changed-root-private-authority"

    task_root = _node("task:normative-intent", authority="caller.private")
    replaced = build_dependency_snapshot(
        candidate=candidate,
        source_index=snapshot.sourceIndex,
        nodes=(*snapshot.nodes, task_root),
        edges=snapshot.edges,
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(replaced), checkers=(CHECK_NAME,)
        )
    assert _reason(caught) == "task-node-private-authority"

    moved = candidate.model_copy(
        update={"code": candidate.code.model_copy(update={"candidateTree": "e" * 40})}
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate, replacement=moved),
            StaticDependencies(snapshot),
            checkers=(CHECK_NAME,),
        )
    assert _reason(caught) == "candidate-moved-during-compilation"


def test_compiler_has_no_user_filename_or_full_scan_authority_parameter() -> None:
    parameters = set(inspect.signature(compile_scope_manifest).parameters)

    assert parameters == {"authority", "dependencies", "checkers"}
    assert not parameters & {"only", "paths", "filenames", "fallback", "full_scan"}
    assert set(inspect.signature(ContractScopeAuthority).parameters) == {"contract"}


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (
            TaskObservationPair(base=_task_observation(), candidate=None),
            "task-candidate-unavailable",
        ),
        (
            TaskObservationPair(
                base=_task_observation(), candidate=_task_observation(topology=None)
            ),
            "task-candidate-topology-unavailable",
        ),
        (
            TaskObservationPair(
                base=_task_observation(),
                candidate=_task_observation(intent=missing_task_intent()),
            ),
            "task-candidate-intent-unavailable",
        ),
    ],
)
def test_candidate_side_missing_r01_or_r02_authority_is_scope_unproven(
    task: TaskObservationPair,
    expected: str,
) -> None:
    candidate = _candidate(task=task)
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(_snapshot(candidate)),
            checkers=(CHECK_NAME,),
        )
    assert _reason(caught) == expected


def test_task_root_must_match_the_contract_pair_root() -> None:
    observation = _task_observation(root="/coord/tasks/repo/other")
    candidate = _candidate(task=TaskObservationPair(base=observation, candidate=observation))

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(_snapshot(candidate)),
            checkers=(CHECK_NAME,),
        )

    assert _reason(caught) == "task-root-pair-mismatch"


def test_snapshot_and_individual_edge_digests_must_self_verify() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    corrupt_snapshot = snapshot.model_copy(update={"snapshotDigest": "0" * 64})
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(corrupt_snapshot),
            checkers=(CHECK_NAME,),
        )
    assert _reason(caught) == "dependency-snapshot-digest-invalid"

    first = snapshot.edges[0].model_copy(update={"authorityNamespace": "caller.private"})
    corrupt_edge = build_dependency_snapshot(
        candidate=candidate,
        source_index=snapshot.sourceIndex,
        nodes=snapshot.nodes,
        edges=(first, *snapshot.edges[1:]),
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(corrupt_edge),
            checkers=(CHECK_NAME,),
        )
    assert _reason(caught) == "dependency-edge-invalid"


def test_task_topology_and_intent_changes_are_canonical_roots() -> None:
    base = _task_observation(topology="a", intent=TaskIntentIdentity(digest="b" * 64))
    current = _task_observation(topology="c", intent=TaskIntentIdentity(digest="d" * 64))
    candidate = _candidate(task=TaskObservationPair(base=base, candidate=current))
    snapshot = _snapshot(candidate)

    manifest = compile_scope_manifest(
        StaticAuthority(candidate), StaticDependencies(snapshot), checkers=(CHECK_NAME,)
    )

    assert manifest.changedRoots == (
        "code:src/a.py",
        "task:normative-intent",
        "task:semantic-topology",
    )


def test_added_root_covers_absent_old_path_and_requires_an_owner_node() -> None:
    candidate = _candidate()
    added = GitPathChange(status="added", newPath="src/new.py", newBlob="a" * 40)
    candidate = candidate.model_copy(
        update={"code": candidate.code.model_copy(update={"changes": (added,)})}
    )
    snapshot = build_dependency_snapshot(
        candidate=candidate,
        source_index=SourceIndexObservation(
            snapshotId="a" * 64,
            codeRoot=candidate.code.root,
            memoryRoot=candidate.memory.root,
            candidateDigest=candidate.digest,
        ),
        nodes=(),
        edges=(),
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(snapshot), checkers=(CHECK_NAME,)
        )

    assert _reason(caught) == "changed-root-unclassified"


def test_missing_dependency_endpoint_fails_before_closure() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    orphan = dependency_edge(
        "code:src/a.py",
        "memory:onboarding/missing.md",
        "source-to-file-sidecar",
        ("missing target fixture",),
    )
    invalid = build_dependency_snapshot(
        candidate=candidate,
        source_index=snapshot.sourceIndex,
        nodes=snapshot.nodes,
        edges=(*snapshot.edges, orphan),
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate), StaticDependencies(invalid), checkers=(CHECK_NAME,)
        )

    assert _reason(caught) == "dependency-edge-endpoint-missing"


def test_reverse_closure_terminates_when_an_edge_targets_an_already_selected_node() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    cycle = dependency_edge(
        "memory:onboarding/src/a.py.md",
        "code:src/a.py",
        "route-index-dependency",
        ("cycle fixture",),
    )
    cyclic = build_dependency_snapshot(
        candidate=candidate,
        source_index=snapshot.sourceIndex,
        nodes=snapshot.nodes,
        edges=(*snapshot.edges, cycle),
    )

    manifest = compile_scope_manifest(
        StaticAuthority(candidate), StaticDependencies(cyclic), checkers=(CHECK_NAME,)
    )

    assert cycle in manifest.selectedEdges


@pytest.mark.parametrize("field", ["nodes", "edges"])
def test_dependency_population_must_be_sorted_and_unique(field: str) -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    values = getattr(snapshot, field)
    noncanonical = snapshot.model_copy(update={field: tuple(reversed(values))})

    with pytest.raises(ScopeUnprovenError) as caught:
        compile_scope_manifest(
            StaticAuthority(candidate),
            StaticDependencies(noncanonical),
            checkers=(CHECK_NAME,),
        )

    assert _reason(caught) == f"dependency-{field}-noncanonical"
