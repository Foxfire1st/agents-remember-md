"""Focused closure and fail-closed tests for the R06 scope compiler."""

from __future__ import annotations

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
)
from agents_remember.memory_quality.incremental_scope.affected_planning import (
    AffectedClosureAdmission,
    compile_affected_closure_plan,
)
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
