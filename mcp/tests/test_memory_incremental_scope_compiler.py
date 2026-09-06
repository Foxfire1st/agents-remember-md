"""Focused closure and fail-closed tests for the R06 scope compiler."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
    GateCertificateIdentity,
)
from agents_remember.memory_quality.check import AVAILABLE_CHECKS, DRIFT_CHECK_NAME
from agents_remember.memory_quality.incremental_scope import affected_planning
from agents_remember.memory_quality.incremental_scope.affected_execution import (
    AffectedClosureExecution,
    execute_affected_closure,
    plan_affected_subresult_reuse,
)
from agents_remember.memory_quality.incremental_scope.affected_models import (
    AffectedClosurePlan,
    AffectedUnitPlan,
    AffectedUnitResult,
)
from agents_remember.memory_quality.incremental_scope.affected_planning import (
    AffectedClosureAdmission,
    compile_affected_closure_plan,
)
from agents_remember.memory_quality.incremental_scope.candidate import ContractScopeAuthority
from agents_remember.memory_quality.incremental_scope.compiler import (
    build_dependency_snapshot,
    compile_scope_manifest,
    dependency_edge,
)
from agents_remember.memory_quality.incremental_scope.errors import (
    GateFiveClosureRefusedError,
    ScopeUnprovenError,
)
from agents_remember.memory_quality.incremental_scope.execution_registry import (
    checker_execution_registry_version,
)
from agents_remember.memory_quality.incremental_scope.models import (
    DependencySnapshot,
    GitPathChange,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeManifest,
    ScopeNode,
    SourceIndexObservation,
    TaskObservationPair,
    canonical_digest,
)
from agents_remember.memory_quality.incremental_scope.registry import checker_scope_registry
from agents_remember.memory_quality.style.citations.range_resolution import CHECK_NAME
from agents_remember.models.certification.base import GateId
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document import CanonicalTaskObservation
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


_R07_GATE_PREFIX: tuple[GateId, ...] = (1, 2, 3, 4)


@dataclass
class R07StableAuthority:
    candidate: ScopeCandidateIdentity
    replacement: ScopeCandidateIdentity | None = None
    calls: int = 0

    def observe(self) -> ScopeCandidateIdentity:
        self.calls += 1
        if self.replacement is not None and self.calls > 1:
            return self.replacement
        return self.candidate


@dataclass(frozen=True)
class R07StaticDependencies:
    snapshot: DependencySnapshot

    def observe(self, candidate: ScopeCandidateIdentity) -> DependencySnapshot:
        del candidate
        return self.snapshot


@dataclass
class R07RecordingExecutor:
    ok: bool = True
    version: str = checker_execution_registry_version()
    calls: list[str] | None = None

    @property
    def registry_version(self) -> str:
        return self.version

    def execute(self, plan: AffectedClosurePlan, unit) -> dict[str, object]:
        del plan
        if self.calls is None:
            self.calls = []
        self.calls.append(unit.document)
        return {
            "check": unit.checker,
            "status": "checked",
            "ok": self.ok,
            "filesChecked": 1,
            "findingCount": 0 if self.ok else 1,
            "findings": [] if self.ok else [{"code": "broken-citation"}],
        }


def _r07_candidate(*, code_tree: str = "4" * 40, memory_tree: str = "7" * 40):
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
    observation = CanonicalTaskObservation(
        taskRoot="/coord/tasks/repo/master",
        taskDocumentRef=TaskDocumentRef(repository="repo", path="master/leaf.json"),
        sourceDigest="a" * 64,
        sourceAuthorityNamespace="agents-remember.task-document-source",
        sourceValidatorVersion="fixture/v1",
        semanticTopologyDigest="b" * 64,
        taskIntent=TaskIntentIdentity(digest="c" * 64),
    )
    return ScopeCandidateIdentity(
        pairIdentity=pair,
        code=GitTreeDelta(
            namespace="code",
            root=pair.codeRoot,
            baseTree="3" * 40,
            candidateTree=code_tree,
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
            candidateTree=memory_tree,
            changes=(),
        ),
        task=TaskObservationPair(base=observation, candidate=observation),
    )


def _r07_node(node_id: str) -> ScopeNode:
    return ScopeNode(
        nodeId=node_id,
        contentDigest=canonical_digest({"node": node_id}),
        authorityNamespace=(
            "agents-remember.git-code-tree"
            if node_id.startswith("code:")
            else "agents-remember.git-memory-tree"
        ),
        validatorVersion="fixture/v1",
        reasons=("owner fixture",),
    )


def _r07_scope(candidate: ScopeCandidateIdentity) -> ScopeManifest:
    nodes = (
        _r07_node("code:src/a.py"),
        _r07_node("memory:onboarding/claims.md"),
        _r07_node("memory:onboarding/src/a.py.md"),
        _r07_node("memory:onboarding/src/overview.index.json"),
        _r07_node("memory:onboarding/src/overview.md"),
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
            "memory:onboarding/src/overview.md",
            "memory:onboarding/src/overview.index.json",
            "route-index-dependency",
            ("index",),
        ),
    )
    source_index = SourceIndexObservation(
        snapshotId="e" * 64,
        codeRoot=candidate.code.root,
        memoryRoot=candidate.memory.root,
        candidateDigest=candidate.digest,
    )
    snapshot = build_dependency_snapshot(
        candidate=candidate,
        source_index=source_index,
        nodes=nodes,
        edges=edges,
    )
    return compile_scope_manifest(
        R07StableAuthority(candidate),
        R07StaticDependencies(snapshot),
        checkers=AVAILABLE_CHECKS,
    )


def _r07_fake_certificates() -> tuple[
    tuple[GateCertificate, ...], tuple[GateCertificateIdentity, ...]
]:
    identities = tuple(
        GateCertificateIdentity(gate=gate, certificateDigest=str(gate) * 64)
        for gate in _R07_GATE_PREFIX
    )
    certificates = tuple(
        cast(
            GateCertificate,
            SimpleNamespace(
                semanticEnvelope=SimpleNamespace(gate=identity.gate),
                identity=identity,
            ),
        )
        for identity in identities
    )
    return certificates, identities


def _r07_admission(
    monkeypatch: pytest.MonkeyPatch,
    candidate: ScopeCandidateIdentity,
    *,
    first_gate: GateId | None = 5,
    changes: tuple[CertificateInputChange, ...] = (),
) -> AffectedClosureAdmission:
    certificates, identities = _r07_fake_certificates()

    def reuse(*args, **kwargs) -> CertificateReusePlan:
        del args, kwargs
        reused = identities if first_gate == 5 else ()
        invalidated = (5,) if first_gate == 5 else (1, 2, 3, 4, 5)
        return CertificateReusePlan(
            reusedCertificates=reused,
            invalidatedGates=invalidated,
            firstGateToRun=first_gate,
            finalizationRevalidationRequired=True,
            zeroGateStarts=first_gate is None,
        )

    monkeypatch.setattr(affected_planning, "plan_certificate_reuse", reuse)
    certificate_admission = cast(
        CertificationAdmissionManifest,
        SimpleNamespace(
            semanticEnvelope=SimpleNamespace(
                candidateCodeTree=SimpleNamespace(value=candidate.code.candidateTree)
            )
        ),
    )
    return AffectedClosureAdmission(
        candidateAuthority=R07StableAuthority(candidate),
        certificationAdmission=certificate_admission,
        gateCertificates=certificates,
        changes=changes,
    )


def _r07_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ScopeCandidateIdentity, AffectedClosurePlan]:
    candidate = _r07_candidate()
    plan = compile_affected_closure_plan(
        _r07_admission(monkeypatch, candidate),
        _r07_scope(candidate),
    )
    return candidate, plan


def _r07_reason(error: pytest.ExceptionInfo[GateFiveClosureRefusedError]) -> str:
    return error.value.failure.code


def test_r07_plan_executes_only_incremental_documents_and_keeps_six_final_checks_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)

    assert plan.candidateDigest == candidate.digest
    assert plan.codeTree == candidate.code.candidateTree
    assert len(plan.pendingFinalFull) == 6
    assert {item.disposition for item in plan.pendingFinalFull} == {"pending-final-full"}
    assert len(plan.units) == 3
    assert {item.document for item in plan.units} == {
        "claims.md",
        "src/a.py.md",
        "src/overview.md",
    }
    claims = next(item for item in plan.units if item.document == "claims.md")
    assert {item.nodeId for item in claims.dependencies} == {
        "code:src/a.py",
        "memory:onboarding/src/a.py.md",
    }
    assert tuple(item.node.nodeId for item in plan.members) == tuple(
        item.nodeId for item in plan.scope.selectedNodes
    )
    assert plan.acceptanceEligible is False
    assert plan.fullFinalRequired is True


def test_r07_execution_publishes_every_member_and_never_promotes_incremental_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    executor = R07RecordingExecutor()
    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), executor),
        plan,
    )

    assert result.incrementalMemoryReady is True
    assert result.terminalStatus == "pass"
    assert result.closeoutReady is False
    assert result.acceptanceEligible is False
    assert result.fullFinalRequired is True
    assert len(result.memberResults) == len(plan.scope.selectedNodes)
    assert {item.disposition for item in result.memberResults} == {
        "checked",
        "dependency-input",
    }
    assert set(executor.calls or ()) == {item.document for item in plan.units}
    assert result.pendingFinalFull == plan.pendingFinalFull


def test_r07_hard_finding_stays_failed_and_malformed_result_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    failed = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor(ok=False)),
        plan,
    )
    assert failed.terminalStatus == "fail"
    assert failed.incrementalMemoryReady is False
    assert {item.status for item in failed.subresults} == {"fail"}

    class EmptySuccess(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan
            return {"check": unit.checker, "status": "checked", "ok": True}

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), EmptySuccess()),
            plan,
        )
    assert _r07_reason(caught) == "checker-result-unproven"

    class ContradictorySuccess(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan
            return {
                "check": unit.checker,
                "status": "checked",
                "ok": True,
                "filesChecked": 1,
                "findingCount": 1,
                "findings": [{"code": "contradiction"}],
            }

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), ContradictorySuccess()),
            plan,
        )
    assert _r07_reason(caught) == "checker-result-unproven"


def test_r07_blocked_unit_preserves_code_and_blocks_aggregate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)

    class BlockedExecutor(R07RecordingExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            del plan
            return {
                "check": unit.checker,
                "status": "blocked",
                "ok": False,
                "code": "source-index-unavailable",
                "filesChecked": 0,
                "findingCount": 0,
                "findings": [],
            }

    blocked = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), BlockedExecutor()),
        plan,
    )
    assert blocked.terminalStatus == "blocked"
    assert blocked.incrementalMemoryReady is False
    assert {item.code for item in blocked.subresults} == {"source-index-unavailable"}

    class UncodedBlocked(BlockedExecutor):
        def execute(self, plan, unit) -> dict[str, object]:
            result = dict(super().execute(plan, unit))
            result.pop("code")
            return result

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(R07StableAuthority(candidate), UncodedBlocked()),
            plan,
        )
    assert _r07_reason(caught) == "checker-result-code-missing"


def test_r07_unchanged_interruption_reuses_exact_passes_without_executing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    first = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor()),
        plan,
    )
    executor = R07RecordingExecutor()
    resumed = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), executor),
        plan,
        first.subresults,
    )

    assert executor.calls is None
    assert resumed.subresults == first.subresults
    assert resumed.executedUnitDigests == ()
    assert resumed.reusedUnitDigests == tuple(item.unitDigest for item in plan.units)
    assert plan_affected_subresult_reuse(plan, first.subresults).unitsToExecute == ()


def test_r07_memory_change_reuses_only_units_with_identical_dependency_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    first = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor()),
        plan,
    )
    changed_scope = plan.scope.model_copy(
        update={
            "selectedNodes": tuple(
                item.model_copy(update={"contentDigest": "f" * 64})
                if item.nodeId == "memory:onboarding/src/a.py.md"
                else item
                for item in plan.scope.selectedNodes
            )
        }
    )
    changed_scope = _r07_redigest_scope(changed_scope)
    changed_candidate = _r07_candidate(memory_tree="8" * 40)
    changed_scope = _r07_retarget_scope(changed_scope, changed_candidate)
    changed_plan = compile_affected_closure_plan(
        _r07_admission(
            monkeypatch,
            changed_candidate,
            changes=(CertificateInputChange(changeClass="memory-onboarding", reason="repair"),),
        ),
        changed_scope,
    )
    reuse = plan_affected_subresult_reuse(changed_plan, first.subresults)

    assert len(reuse.reusedUnitDigests) == 1
    assert len(reuse.unitsToExecute) == 2
    assert reuse.ignoredPriorResultDigests


def _r07_redigest_scope(scope: ScopeManifest) -> ScopeManifest:
    payload = scope.model_dump(mode="json", by_alias=True, exclude={"manifestDigest"})
    return scope.model_copy(update={"manifestDigest": canonical_digest(payload)})


def _r07_retarget_scope(
    scope: ScopeManifest,
    candidate: ScopeCandidateIdentity,
) -> ScopeManifest:
    source_index = scope.sourceIndex.model_copy(update={"candidateDigest": candidate.digest})
    return _r07_redigest_scope(
        scope.model_copy(update={"candidateDigest": candidate.digest, "sourceIndex": source_index})
    )


def test_r07_incomplete_scope_code_repair_and_candidate_motion_refuse_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _r07_candidate()
    scope = _r07_scope(candidate)
    incomplete = _r07_redigest_scope(
        scope.model_copy(update={"checkerPolicies": scope.checkerPolicies[:-1]})
    )
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        compile_affected_closure_plan(_r07_admission(monkeypatch, candidate), incomplete)
    assert _r07_reason(caught) == "affected-checker-population-incomplete"

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        compile_affected_closure_plan(
            _r07_admission(monkeypatch, candidate, first_gate=1),
            scope,
        )
    assert _r07_reason(caught) == "gate-certificate-prefix-invalidated"

    moved = _r07_candidate(memory_tree="8" * 40)
    admission = _r07_admission(monkeypatch, candidate)
    admission = AffectedClosureAdmission(
        candidateAuthority=R07StableAuthority(candidate, replacement=moved),
        certificationAdmission=admission.certificationAdmission,
        gateCertificates=admission.gateCertificates,
    )
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        compile_affected_closure_plan(admission, scope)
    assert _r07_reason(caught) == "candidate-moved-during-affected-planning"


def test_r07_executor_registry_and_post_plan_candidate_are_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(
                R07StableAuthority(candidate),
                R07RecordingExecutor(version="0" * 64),
            ),
            plan,
        )
    assert _r07_reason(caught) == "checker-execution-registry-stale"

    moved = _r07_candidate(memory_tree="8" * 40)
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        execute_affected_closure(
            AffectedClosureExecution(
                R07StableAuthority(candidate, replacement=moved), R07RecordingExecutor()
            ),
            plan,
        )
    assert _r07_reason(caught) == "affected-candidate-stale"


def test_r07_conflicting_prior_result_authority_is_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor()),
        plan,
    ).subresults[0]
    payload = result.model_dump(mode="json", by_alias=True, exclude={"resultDigest"})
    payload["code"] = "different-pass"
    conflicting = AffectedUnitResult(
        **payload,
        resultDigest=canonical_digest(payload),
    )

    with pytest.raises(GateFiveClosureRefusedError) as caught:
        plan_affected_subresult_reuse(plan, (result, conflicting))
    assert _r07_reason(caught) == "subresult-authority-conflict"

    forged = result.model_copy(update={"resultDigest": "f" * 64})
    with pytest.raises(GateFiveClosureRefusedError) as caught:
        plan_affected_subresult_reuse(plan, (forged,))
    assert _r07_reason(caught) == "subresult-invalid"


def test_r07_plan_and_aggregate_models_refuse_rebound_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, plan = _r07_plan(monkeypatch)
    unit = plan.units[0]
    unit_payload = unit.model_dump(mode="json", by_alias=True, exclude={"unitDigest"})
    unit_payload["document"] = "other.md"
    rebound = AffectedUnitPlan(
        **unit_payload,
        unitDigest=canonical_digest(unit_payload),
    )
    rebound_plan = plan.model_copy(update={"units": (rebound, *plan.units[1:])})
    with pytest.raises(
        ValueError,
        match="affected-unit document must name its exact selected memory node",
    ):
        rebound_plan._require_exact_incremental_units()

    result = execute_affected_closure(
        AffectedClosureExecution(R07StableAuthority(candidate), R07RecordingExecutor()),
        plan,
    )
    checked_index = next(
        index for index, member in enumerate(result.memberResults) if member.status == "pass"
    )
    member_results = list(result.memberResults)
    member_results[checked_index] = member_results[checked_index].model_copy(
        update={"status": "fail"}
    )
    rebound_result = result.model_copy(update={"memberResults": tuple(member_results)})
    with pytest.raises(
        ValueError,
        match="member result disposition and status must derive from its units",
    ):
        rebound_result._require_member_results(
            {item.unit.unitDigest: item for item in rebound_result.subresults}
        )
